# CONTEXTO_SESION.md — notas de sesión sobre gobernanza

## Limitaciones conocidas

El gate de Bash matchea texto del comando, no rutas.
Es heurístico y no puede ser completo: un script con
nombre no contemplado (pagar.py, cerrar.py) pasa el
filtro. La protección estructural real está en
Write/Edit, que matchea rutas bajo bot/. No confiar
en el gate de Bash como única barrera antes de una
operación que toque saldos.
