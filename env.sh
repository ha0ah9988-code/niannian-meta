# niannian-meta 环境变量快速配置
# 此文件会被 run.py 自动加载，无需手动 source

# LLM 配置
export LLM_API_KEY="sk-kJFBqTU4hZvG9kmWBo2XknXWb5yxX1hpuV3tBzXhXDHr00wRdOeQAroqpSosvhsh"
export LLM_BASE_URL="https://opencode.ai/zen/go/v1"
export LLM_MODEL="deepseek-v4-flash"

# 代理（如需走代理访问 API，取消注释并改端口）
export HTTP_PROXY="http://127.0.0.1:7890"
export HTTPS_PROXY="http://127.0.0.1:7890"

echo "✓ niannian-meta 环境已配置"
echo "  Model: $LLM_MODEL"
