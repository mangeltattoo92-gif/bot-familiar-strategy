# Registro de ahorro de tokens

**Limitacion honesta:** no tengo un contador de tokens en vivo que pueda
consultar por codigo -- esto NO es medicion automatica, es un registro
manual de decisiones que evitan gasto grande, anotado cuando pasa. No lo
trates como una metrica precisa dia a dia, es un log de eventos, no un
dashboard.

**Regla:** cuando evite un patron caro (fan-out multi-agente, re-lectura
innecesaria de archivos grandes, relanzar algo que ya se hizo) lo anoto
aca con la fecha y una estimacion aproximada. Ver tambien
`feedback_trading_bot_no_multiagent` en mi memoria entre sesiones (fuera
de este repo) -- esa es la version "regla permanente", esto es el log de
cuando se aplico.

---

## 2026-09-09

- **Evitado:** `/code-review max --fix` en modo multi-agente (10
  sub-agentes en paralelo) para auditar ~10 archivos propios. 2 de 10
  llegaron a terminar antes de que el resto fallara por rate limit,
  gastando **304,097 tokens entre los dos** -- si los 10 hubieran
  completado, facil 1.5M+ tokens.
- **En su lugar:** lei los 10 archivos yo mismo, en una sola pasada, y
  aplique 5 fixes reales encontrados por los 2 sub-agentes que si
  terminaron (sin gastar tokens extra en volver a analizar lo que ya
  habian encontrado).
- **Politica adoptada:** no volver a usar fan-out multi-agente en este
  proyecto salvo pedido explicito del usuario -- ver `INCIDENTS.md` #7.
