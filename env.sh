# niannian-meta 环境变量快速配置
# 此文件会被 run.py 自动加载，无需手动 source

# 默认 LLM（opencode DeepSeek）
export LLM_API_KEY="sk-kJFBqTU4hZvG9kmWBo2XknXWb5yxX1hpuV3tBzXhXDHr00wRdOeQAroqpSosvhsh"
export LLM_BASE_URL="https://opencode.ai/zen/go/v1"
export LLM_MODEL="deepseek-v4-flash"

# 备用 LLM（Mimo v2.5）
export LLM2_API_KEY="sk-c1wb3qndc7ts89cleu10ti2t0jsnakjnsin5dndyfhiohzkc"
export LLM2_BASE_URL="https://api.xiaomimimo.com/v1"
export LLM2_MODEL="mimo-v2.5-pro"

echo "✓ niannian-meta 环境已配置"
echo "  Model: $LLM_MODEL"
