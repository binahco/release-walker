from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import yaml
from agent_loop import Action, AgentTrace, ToolRegistry, run_agent
from cache_ratelimit import CachedProvider, RateLimiter, RealClock, ThrottledProvider
from llm_client import CompletionRequest, CompletionResult, LlmClient, ReplayProvider, Span
from llm_client.providers.opencode_cli import OpenCodeCLI
from parser_io import parse
from schema_validate import SchemaRegistry
from secure_base import sanitize_for_prompt
from test_kit import EvalCase, EvalDataset, run

from .models import ReleaseNotes

DEFAULT_MODEL = "opencode/big-pickle"
SCHEMA_ID = "release-notes-v1"
PROMPT_ID = "release-notes-generator"
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROMPT = ROOT / "prompts" / "release-notes-generator.md"
DEFAULT_DATASET = ROOT / "evals" / "release-notes-generator.jsonl"


# --------------------------------------------------------------------------
# herramientas del gate: lectura y git, nada escribe
# --------------------------------------------------------------------------
def build_registry(repo: Path, version: str) -> ToolRegistry:
    registry = ToolRegistry()

    def _pyproject_version(_args: dict) -> str:
        m = re.search(r'^version\s*=\s*"([^"]+)"', (repo / "pyproject.toml").read_text(), re.MULTILINE)
        return m.group(1) if m else "?"

    def _changelog_has(_args: dict) -> str:
        doc = parse(repo / "CHANGELOG.md")
        return "si" if any(s.title.startswith(f"v{version} ") or s.title.startswith(f"{version} ") for s in doc.sections) else "no"

    def _tag_exists(_args: dict) -> str:
        proc = subprocess.run(["git", "tag", "-l", f"v{version}"], cwd=repo, capture_output=True, text=True, check=False)
        return "si" if proc.stdout.strip() else "no"

    def _tree_clean(_args: dict) -> str:
        proc = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=False)
        lineas = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        return "si" if not lineas else f"no ({len(lineas)} pendientes)"

    def _changelog_section(_args: dict) -> str:
        doc = parse(repo / "CHANGELOG.md")
        for s in doc.sections:
            if s.title.startswith(f"v{version} ") or s.title.startswith(f"{version} "):
                return s.body[:1500]
        return "(sin sección para esta versión)"

    registry.register("pyproject_version", "versión declarada en pyproject.toml", _pyproject_version)
    registry.register("changelog_has", "¿existe sección de la versión en CHANGELOG.md?", _changelog_has)
    registry.register("tag_exists", "¿existe el tag local v{versión}?", _tag_exists)
    registry.register("tree_clean", "¿árbol git limpio?", _tree_clean)
    registry.register("changelog_section", "texto de la sección del CHANGELOG de la versión", _changelog_section)
    return registry


CHECKS: tuple[tuple[str, str, Callable[[str], bool], str], ...] = (
    ("pyproject_version", "versión en pyproject", lambda out, v: out == v, "≠ {esperado}"),
    ("changelog_has", "sección en CHANGELOG", lambda out, v: out == "si", ""),
    ("tag_exists", "tag local", lambda out, v: out == "si", ""),
    ("tree_clean", "árbol limpio", lambda out, v: out == "si", ""),
)


def brain_gate(version: str):
    """Cerebro por guion: los 4 checks, luego el texto del changelog, luego termina."""
    cola = [Action(razon=f"check {nombre}", tool=tool, args={}) for tool, nombre, _, _ in CHECKS]
    cola.append(Action(razon="leer el changelog de la versión", tool="changelog_section", args={}))
    estado = {"i": 0}

    def brain(_vista: dict) -> Action:
        if estado["i"] < len(cola):
            accion = cola[estado["i"]]
            estado["i"] += 1
            return accion
        return Action(final="gate completo")

    return brain


def gate(repo: Path, version: str, *, confirm: Callable | None = None) -> tuple[AgentTrace, list[dict], str]:
    """Corre el gate y devuelve (trace, resultados, texto del changelog)."""
    trace = run_agent(
        f"gate de release v{version} en {repo.name}",
        registry=build_registry(repo, version),
        brain=brain_gate(version),
        confirm=confirm,
        max_steps=8,
    )
    salidas = {s.accion.tool: s.salida for s in trace.steps if s.estado == "ok"}
    resultados = []
    for tool, nombre, ok_fn, _fmt in CHECKS:
        salida = salidas.get(tool, "no ejecutado")
        resultados.append({"check": nombre, "ok": salida != "no ejecutado" and ok_fn(salida, version), "detalle": salida})
    return trace, resultados, salidas.get("changelog_section", "(sin sección para esta versión)")


def gate_text(resultados: list[dict]) -> str:
    return "\n".join(f"- {r['check']}: {'ok' if r['ok'] else 'FALLÓ'} ({r['detalle']})" if r["detalle"] else f"- {r['check']}: {'ok' if r['ok'] else 'FALLÓ'}" for r in resultados)


# --------------------------------------------------------------------------
# notas LLM validadas
# --------------------------------------------------------------------------
def load_prompt(path: Path) -> tuple[str, str, str, Path]:
    text = path.read_text()
    if not text.startswith("---"):
        raise SystemExit(f"{path}: falta frontmatter")
    _, frontmatter, body = text.split("---", 2)
    data = yaml.safe_load(frontmatter)
    eval_path = ROOT / data["eval"] if not Path(data["eval"]).is_absolute() else Path(data["eval"])
    return data["id"], data["version"], body.strip(), eval_path


def render_notes(prompt_id: str, prompt_version: str, variables: dict) -> list[dict]:
    if prompt_id == PROMPT_ID:
        _, _, body, _ = load_prompt(DEFAULT_PROMPT)
        system_part = body.split("## Sistema\n", 1)[1].split("## Usuario\n", 1)[0].strip()
        user_part = body.split("## Usuario\n", 1)[1].strip().format(**variables)
        return [{"role": "system", "content": system_part}, {"role": "user", "content": user_part}]
    payload = {"prompt_id": prompt_id, "prompt_version": prompt_version, "variables": variables}
    return [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]


def build_emitter(span_file: Path | None):
    def emit(span: Span, _result) -> None:
        if span_file is not None:
            with span_file.open("a") as handle:
                handle.write(span.as_jsonl() + "\n")
        else:
            sys.stderr.write(span.as_jsonl() + "\n")

    return emit


def build_client(inner_provider, *, clock=None, span_file: Path | None = None) -> dict:
    clock = RealClock() if clock is None else clock
    cached = CachedProvider(inner_provider, ttl_seconds=3600.0, clock=clock)
    limiter = RateLimiter(rpm=120, burst=20, clock=clock)
    provider = ThrottledProvider(cached, limiter)
    registry = SchemaRegistry()
    registry.register(SCHEMA_ID, ReleaseNotes)
    client = LlmClient(
        provider,
        consumer_repo="release-walker",
        model_aliases={"fast": DEFAULT_MODEL},
        renderer=render_notes,
        validator=registry.make_validator(SCHEMA_ID),
        emitter=build_emitter(span_file),
    )
    return {"client": client, "cached": cached, "limiter": limiter}


def notes(client: LlmClient, version: str, resultados: list[dict], changelog: str) -> ReleaseNotes:
    texto_gate = sanitize_for_prompt(gate_text(resultados)).text.replace("{", "(").replace("}", ")")
    texto_cl = sanitize_for_prompt(changelog).text.replace("{", "(").replace("}", ")")
    result = client.complete(
        CompletionRequest(
            prompt_id=PROMPT_ID,
            prompt_version="0.1.0",
            variables={"version": version, "gate": texto_gate, "changelog": texto_cl},
            model_alias="fast",
            response_schema=SCHEMA_ID,
            tags=["release-walker", "week-22"],
        )
    )
    if not result.validation.ok:
        raise RuntimeError("; ".join(result.validation.errors))
    return result.parsed


def judge_for(client: LlmClient) -> Callable[[EvalCase], CompletionResult]:
    def judge(case: EvalCase) -> CompletionResult:
        return client.complete(
            CompletionRequest(
                prompt_id=case.prompt_id,
                prompt_version=case.prompt_version,
                variables=case.input,
                model_alias="fast",
                response_schema=SCHEMA_ID,
                tags=["release-walker", "week-22"],
            )
        )

    return judge


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="release-walker", description="Gate de release con agente + notas validadas (semana 22).")
    parser.add_argument("command", nargs="?", default="check", choices=["check", "record", "replay-check"])
    parser.add_argument("--repo", default="../llm-dev-core", help="repo del core")
    parser.add_argument("--version", default=None, help="versión a verificar (p. ej. 1.3.0); obligatoria solo para check")
    args = parser.parse_args(argv)

    if args.command == "check" and not args.version:
        parser.error("check exige --version")

    if args.command == "record":
        dataset = EvalDataset.from_jsonl(DEFAULT_DATASET)
        provider = ReplayProvider(ROOT / "cassettes", record=True, inner=OpenCodeCLI(DEFAULT_MODEL))
        out = run(dataset, judge_for(build_client(provider)["client"]), mode="full", threshold=1.0)
        print(f"grabadas {len(dataset.cases)} respuestas; pass={out.passed}/{out.total}")
        return 0 if out.threshold_ok else 1

    if args.command == "replay-check":
        dataset = EvalDataset.from_jsonl(DEFAULT_DATASET)
        provider = ReplayProvider(ROOT / "cassettes", record=False)
        out = run(dataset, judge_for(build_client(provider)["client"]), mode="full", threshold=1.0)
        print(f"replay: pass={out.passed}/{out.total} threshold_ok={out.threshold_ok}")
        return 0 if out.threshold_ok else 1

    trace, resultados, changelog = gate(Path(args.repo), args.version)
    salida = {"version": args.version, "paro": trace.paro, "gate": resultados}
    ready = trace.paro == "done" and all(r["ok"] for r in resultados)
    if trace.paro == "done":
        card = notes(build_client(OpenCodeCLI(DEFAULT_MODEL))["client"], args.version, resultados, changelog)
        salida["notas"] = card.model_dump()
    print(json.dumps(salida, ensure_ascii=False, indent=2))
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
