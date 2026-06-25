# niannian-meta 环境变量快速配置
# 使用: source env.sh

# LLM 配置（从 Hermes 配置读取）
export LLM_API_KEY="sk-kJFBqTU4hZvG9kmWBo2XknXWb5yxX1hpuV3tBzXhXDHr00wRdOeQAroqpSosvhsh"
export LLM_BASE_URL="https://opencode.ai/zen/go/v1"
export LLM_MODEL="deepseek-v4-flash"

echo "✓ niannian-meta 环境已配置"
echo "  Model: $LLM_MODEL"
echo "  API: $LLM_BASE_URL"
