-- 初始化数据库

CREATE TABLE IF NOT EXISTS projects (
    id VARCHAR(50) PRIMARY KEY,
    name VARCHAR(200) NOT NULL,
    client VARCHAR(100),
    status VARCHAR(20) DEFAULT 'pending',
    budget DECIMAL(10, 2) DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tasks (
    id VARCHAR(50) PRIMARY KEY,
    project_id VARCHAR(50) REFERENCES projects(id),
    title VARCHAR(200) NOT NULL,
    description TEXT,
    assigned_to VARCHAR(50),
    status VARCHAR(20) DEFAULT 'pending',
    priority VARCHAR(20) DEFAULT 'medium',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS agent_states (
    agent_id VARCHAR(50) PRIMARY KEY,
    name VARCHAR(50) NOT NULL,
    role VARCHAR(50) NOT NULL,
    state VARCHAR(20) DEFAULT 'idle',
    current_task VARCHAR(50),
    last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS conversations (
    id SERIAL PRIMARY KEY,
    agent_role VARCHAR(50) NOT NULL,
    message TEXT NOT NULL,
    response TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS memories (
    id VARCHAR(50) PRIMARY KEY,
    agent_id VARCHAR(50) NOT NULL,
    agent_role VARCHAR(50),
    content TEXT NOT NULL,
    memory_type VARCHAR(20) DEFAULT 'task',
    metadata JSONB,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_memories_agent ON memories(agent_id);
CREATE INDEX IF NOT EXISTS idx_memories_type ON memories(memory_type);
CREATE INDEX IF NOT EXISTS idx_memories_agent_role ON memories(agent_role);
CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at DESC);

-- 记忆关联图谱（P2: Memory Graph）
CREATE TABLE IF NOT EXISTS memory_edges (
    id SERIAL PRIMARY KEY,
    source_id VARCHAR(50) NOT NULL,
    target_id VARCHAR(50) NOT NULL,
    edge_type VARCHAR(30) NOT NULL DEFAULT 'same_tags',
    weight DECIMAL(3,2) DEFAULT 0.7,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_id, target_id, edge_type)
);

CREATE INDEX IF NOT EXISTS idx_edges_source ON memory_edges(source_id);
CREATE INDEX IF NOT EXISTS idx_edges_target ON memory_edges(target_id);
CREATE INDEX IF NOT EXISTS idx_edges_type ON memory_edges(edge_type);

-- 长期知识向量。维度与百炼 text-embedding-v4 的默认输出一致。
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS agent_knowledge (
    id VARCHAR(64) PRIMARY KEY,
    agent_id VARCHAR(64) NOT NULL,
    agent_role VARCHAR(64) NOT NULL,
    memory_type VARCHAR(64) NOT NULL,
    content TEXT NOT NULL,
    embedding vector(1024) NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_agent_knowledge_role ON agent_knowledge(agent_role);
CREATE INDEX IF NOT EXISTS idx_agent_knowledge_type ON agent_knowledge(memory_type);
CREATE INDEX IF NOT EXISTS idx_agent_knowledge_embedding
    ON agent_knowledge USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS audit_logs (
    id SERIAL PRIMARY KEY,
    agent_id VARCHAR(50) NOT NULL,
    action VARCHAR(100) NOT NULL,
    details JSONB,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
