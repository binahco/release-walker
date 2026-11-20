from __future__ import annotations

import json
import subprocess
from pathlib import Path

from llm_client.provider import ProviderRequest, ProviderResponse
from test_kit import EvalDataset, run as run_evalset

from release_walker.main import DEFAULT_DATASET, build_client, gate, judge_for, notes

STUB_TEXT = json.dumps(
    {"version": "1.3.0", "ready": True, "blockers": [], "notes": "trae agent-loop"},
    ensure_ascii=False,
)


class StubProvider:
    name = "stub"

    def __init__(self, text: str | None = None) -> None:
        self.text = text or STUB_TEXT
        self.calls = 0
        self.last_request: ProviderRequest | None = None

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        self.calls += 1
        self.last_request = request
        return ProviderResponse(text=self.text, model=request.model)

    def stream(self, request: ProviderRequest) -> list[str]:
        self.calls += 1
        return [self.text]


def mini_repo(tmp_path: Path, version: str = "1.3.0", tag: bool = True) -> Path:
    repo = tmp_path / "core"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(f'[project]\nname = "x"\nversion = "{version}"\n')
    (repo / "CHANGELOG.md").write_text(
        "# Changelog\n\n"
        f"## v{version} — 2026-11-20 (semana 21)\n\n- agent-loop.\n\n"
        "## v1.2.0 — 2026-11-20\n\n- diff-engine.\n"
    )
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"], cwd=repo, check=True)
    if tag:
        subprocess.run(["git", "tag", f"v{version}"], cwd=repo, check=True)
    return repo


def test_gate_en_verde_sobre_un_repo_real(tmp_path) -> None:
    trace, resultados, changelog = gate(mini_repo(tmp_path), "1.3.0")
    assert trace.paro == "done"
    assert all(r["ok"] for r in resultados), resultados
    assert "agent-loop" in changelog


def test_gate_detecta_version_changelog_y_tag(tmp_path) -> None:
    repo = mini_repo(tmp_path, version="1.3.0")
    (repo / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "9.9.9"\n')
    subprocess.run(["git", "tag", "-d", "v1.3.0"], cwd=repo, check=True)
    _, resultados, _ = gate(repo, "1.3.0")
    checks = {r["check"]: r for r in resultados}
    assert checks["versión en pyproject"]["ok"] is False and "9.9.9" in checks["versión en pyproject"]["detalle"]
    assert checks["tag local"]["ok"] is False


def test_arbol_sucio_es_bloqueador(tmp_path) -> None:
    repo = mini_repo(tmp_path)
    (repo / "sucio.txt").write_text("x")
    _, resultados, _ = gate(repo, "1.3.0")
    checks = {r["check"]: r for r in resultados}
    assert checks["árbol limpio"]["ok"] is False and "1 pendiente" in checks["árbol limpio"]["detalle"]


def test_la_puerta_rechaza_el_gate(tmp_path) -> None:
    trace, _, _ = gate(mini_repo(tmp_path), "1.3.0", confirm=lambda a: False)
    assert trace.paro == "rechazado"


def test_las_notas_validan_y_el_gate_no_se_inventa(tmp_path) -> None:
    repo = mini_repo(tmp_path)
    _, resultados, changelog = gate(repo, "1.3.0")
    stub = StubProvider()
    card = notes(build_client(stub)["client"], "1.3.0", resultados, changelog)
    assert card.ready is True
    mensaje = json.dumps([m["content"] for m in stub.last_request.messages], ensure_ascii=False)
    assert "agent-loop" in mensaje, "el changelog de la versión no llegó al prompt"


def test_el_judge_evoluciona_el_dataset_completo(tmp_path) -> None:
    dataset = EvalDataset.from_jsonl(DEFAULT_DATASET)
    client = build_client(StubProvider())["client"]
    out = run_evalset(dataset, judge_for(client), mode="full", threshold=1.0)
    assert out.total == 3
    assert out.passed == 3
