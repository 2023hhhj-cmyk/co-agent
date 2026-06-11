#!/bin/bash
# Muzhu-AI 项目部署脚本

SERVER="muzhu-ai"
REMOTE_DIR="/root/muzhu-ai"
LOCAL_DIR="/Users/hhhj/Desktop/co-agent"

echo "=== 开始部署 Muzhu-AI ==="
echo "服务器: $SERVER"
echo "远程目录: $REMOTE_DIR"
echo ""

# 排除的文件和目录
EXCLUDE="--exclude=.git --exclude=.claude --exclude=venv --exclude=__pycache__ --exclude=.env --exclude=product.md --exclude=CLAUDE.md"

# 使用rsync同步文件
echo "=== 同步文件到服务器 ==="
rsync -avz --progress $EXCLUDE -e "ssh -o StrictHostKeyChecking=no" "$LOCAL_DIR/" "$SERVER:$REMOTE_DIR/"

echo ""
echo "=== 同步 Skills 到服务器 ==="
rsync -avz --progress -e "ssh -o StrictHostKeyChecking=no" "$LOCAL_DIR/.claude/skills/" "$SERVER:$REMOTE_DIR/.claude/skills/"

echo ""
echo "=== 文件同步完成 ==="

# 在服务器上执行初始化
echo ""
echo "=== 在服务器上初始化环境 ==="
ssh -o StrictHostKeyChecking=no "$SERVER" << 'REMOTE_SCRIPT'
cd /root/muzhu-ai

# 创建虚拟环境（如果不存在）
if [ ! -d "venv" ]; then
    echo "创建Python虚拟环境..."
    python3 -m venv venv
fi

# 激活虚拟环境并安装依赖
echo "安装Python依赖..."
source venv/bin/activate
pip install -r requirements.txt -q

# 初始化数据库
echo "初始化数据库..."
export PGPASSWORD=muzhu_pg_2024
psql -h localhost -U muzhu -d muzhu_agent -f scripts/init_db.sql 2>/dev/null || echo "数据库表可能已存在"

# 创建启动脚本
cat > start.sh << 'EOF'
#!/bin/bash
cd /root/muzhu-ai
source venv/bin/activate
python main.py
EOF
chmod +x start.sh

echo ""
echo "=== 初始化完成 ==="
echo "使用以下命令启动服务:"
echo "  cd /root/muzhu-ai && ./start.sh"
REMOTE_SCRIPT

echo ""
echo "=== 部署完成 ==="
