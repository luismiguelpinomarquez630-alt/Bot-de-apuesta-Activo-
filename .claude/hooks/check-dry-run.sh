#!/bin/bash
# Hook PreToolUse: bloquea Write/Edit/Bash sobre código de liquidación
# si no existe la marca .claude/DRY_RUN_OK

input=$(cat)
tool_name=$(echo "$input" | jq -r '.tool_name // empty')
file_path=$(echo "$input" | jq -r '.tool_input.file_path // empty')
command=$(echo "$input" | jq -r '.tool_input.command // empty')

PATTERN="settlement_engine|fuente_resultados|cascada_fuentes|score_final_confirmado|payout|liquidacion"

READ_ONLY_PREFIX="^(cat|less|more|head|tail|grep|rg|ls|find|wc|sed -n|awk|git diff|git show|git log|git status|git blame|pytest|python -m pytest)\b"

if [ "$tool_name" = "Write" ] || [ "$tool_name" = "Edit" ]; then
  if echo "$file_path" | grep -qiE "$PATTERN"; then
    if [ ! -f ".claude/DRY_RUN_OK" ]; then
      echo '{"decision": "block", "reason": "Bloqueado: este archivo toca liquidación y no existe .claude/DRY_RUN_OK. Corré el dry-run primero y creá ese archivo marcador (touch .claude/DRY_RUN_OK)."}'
      exit 0
    fi
  fi
elif [ "$tool_name" = "Bash" ]; then
  if echo "$command" | grep -qE "$READ_ONLY_PREFIX"; then
    echo '{"decision": "approve"}'
    exit 0
  fi
  if echo "$command" | grep -qiE "$PATTERN"; then
    if [ ! -f ".claude/DRY_RUN_OK" ]; then
      echo '{"decision": "block", "reason": "Bloqueado: este comando puede escribir sobre codigo/datos de liquidacion y no existe .claude/DRY_RUN_OK. Si es solo lectura, usa cat/git diff/grep, o ajusta READ_ONLY_PREFIX en el hook."}'
      exit 0
    fi
  fi
fi

echo '{"decision": "approve"}'
