# release-walker

Gate de release con agente (semana 22): un walker de `agent-loop` verifica que un
release del core **está realmente listo** — versión en `pyproject`, sección en el
CHANGELOG, tag local, árbol limpio — y redacta las notas validadas a partir del texto
medido. Es el **segundo consumidor de `agent-loop`**: el patrón del walk se repite y
la regla del tercer uso (D12) toma nota.

Nació de un hábito propio: cada release de este reto se preparó a mano con los mismos
cuatro checks. El agente que los corre ahora es el mismo que camina checklists desde
la semana 21 — solo cambiaron las herramientas.

## Demo

```bash
uv sync
uv run release-walker check --repo ../llm-dev-core --version 1.3.0
# {
#   "version": "1.3.0", "paro": "done",
#   "gate": [
#     {"check": "versión en pyproject", "ok": true, "detalle": "1.3.0"},
#     ...
#   ],
#   "notas": {"ready": true, "blockers": [], "notes": "…"}
# }
```

El `ready` y los `blockers` salen del **gate medido**, no de la opinión del modelo:
las notas LLM reciben los resultados y el texto del CHANGELOG de la versión, y
validan contra `release-notes-v1` o fallan. Exit 1 si algo bloquea.

## Cómo funciona

```
release-walker check --version X
  ├─ agent-loop: cerebro por guion → 4 checks + lectura del changelog
  │    ├─ tools: pyproject_version · changelog_has (parser-io) · tag_exists (git) · tree_clean (git)
  │    └─ trace completo del gate — cada check con su salida
  ├─ veredicto: ok/FALLÓ por comparación, no por juicio
  └─ notas LLM validadas (release-notes-v1) sobre gate + changelog saneados
```

- Todo es **lectura**: el gate no crea tags ni commitea — el efecto queda a mano del
  mantenedor (la puerta está antes del efecto, ADR-11 §2).
- El CHANGELOG se lee con `parser-io`: la sección de la versión con su rango de líneas.

## Recicla de

| Módulo | Uso |
|---|---|
| `agent-loop` | segundo uso: el mismo loop del checklist, otras herramientas |
| `parser-io` | la sección del CHANGELOG de la versión, con líneas reales |
| `llm-client` | la llamada de las notas (retry + reparación + span de 20 campos) |
| `schema-validate` | las notas validan contra `release-notes-v1` o fallan |
| `secure-base` | gate y changelog entran saneados al prompt |
| `test-kit` | dataset congelado + replay determinista (D4) |
| `cache-ratelimit` | caché TTL + token bucket alrededor del proveedor |

## Limitaciones

- Cuatro checks fijos: el gate crece cuando un release exija más (y entonces el
  checklist deja de ser guion y se vuelve config).
- No verifica el remoto: tag sin pushear y tag pusheado se ven igual en local — el
  estado público lo sabe `core-smoke` contra PyPI.
- Las notas describen lo que dice el CHANGELOG: si el CHANGELOG miente, las notas
  heredan la mentira (eso lo caza la verificación del baseline, no este gate).

## Roadmap

- [x] Gate de release con agente + notas validadas (sem. 22)
- [ ] Checks remotos: tag en origin, dist publicada en PyPI
- [ ] Gate como tarea de bot-base antes de cada release (con `smoke-bot`)
- [ ] Cerebro LLM que decida el orden e investigue blockers (con confirmación)
