#!/bin/bash
# Hook PreToolUse: bloquea Write/Edit/Bash sobre código de liquidación
# si no existe la marca .claude/DRY_RUN_OK
#
# AJUSTAR: el patrón grep de la línea "if echo..." a los nombres reales
# de tus módulos de settlement/liquidación antes de confiar en esto.

input=$(cat)
file_path=$(echo "$input" | jq -r '.tool_input.file_path // empty')
command=$(echo "$input" | jq -r '.tool_input.command // empty')

# Patrones que identifican código sensible de liquidación.
# Estos son los nombres de módulo definidos en la spec V2 (capa de settlement).
# Si Claude Code nombra los archivos distinto durante la implementación,
# esta lista se ajusta para que coincida ANTES de mergear el primer PR de settlement.
PATTERN="settlement_engine|fuente_resultados|cascada_fuentes|score_final_confirmado|payout|liquidacion"

if echo "$file_path $command" | grep -qiE "$PATTERN"; then
  if [ ! -f ".claude/DRY_RUN_OK" ]; then
    echo '{"decision": "block", "reason": "Bloqueado: este archivo/comando toca liquidación y no existe .claude/DRY_RUN_OK. Corré el dry-run primero y creá ese archivo marcador (touch .claude/DRY_RUN_OK), o si es solo diagnóstico de lectura ignora este bloqueo pidiendo explícitamente una herramienta de solo lectura."}'
    exit 0
  fi
fi

echo '{"decision": "approve"}'
