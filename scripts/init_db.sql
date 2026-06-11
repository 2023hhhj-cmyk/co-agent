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
    content TEXT NOT NULL,
    metadata JSONB,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id SERIAL PRIMARY KEY,
    agent_id VARCHAR(50) NOT NULL,
    action VARCHAR(100) NOT NULL,
    details JSONB,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS transactions (
    id VARCHAR(50) PRIMARY KEY,
    project_id VARCHAR(50) REFERENCES projects(id),
    type VARCHAR(20) NOT NULL,
    amount DECIMAL(10, 2) NOT NULL,
    description TEXT,
    date DATE DEFAULT CURRENT_DATE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
