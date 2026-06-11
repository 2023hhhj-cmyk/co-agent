-- 客服系统数据库表
-- 在现有 muzhu_agent 数据库中执行

-- 客户-项目映射（一个客户可能有多个项目）
CREATE TABLE IF NOT EXISTS cs_customer_projects (
    id SERIAL PRIMARY KEY,
    external_userid VARCHAR(128) NOT NULL,  -- 企业微信外部联系人 ID
    customer_name VARCHAR(128) DEFAULT '',
    project_id VARCHAR(64) NOT NULL,        -- 关联 projects 表
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(external_userid, project_id)
);

-- FAQ 知识库（公司通用知识）
CREATE TABLE IF NOT EXISTS cs_faq (
    id SERIAL PRIMARY KEY,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    category VARCHAR(64) DEFAULT 'general',
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- 客户会话记录（持久化）
CREATE TABLE IF NOT EXISTS cs_conversations (
    id SERIAL PRIMARY KEY,
    external_userid VARCHAR(128) NOT NULL,
    role VARCHAR(16) NOT NULL,        -- 'customer' / 'agent'
    content TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_cs_conv_user ON cs_conversations(external_userid, created_at);

-- 转人工记录
CREATE TABLE IF NOT EXISTS cs_escalations (
    id SERIAL PRIMARY KEY,
    external_userid VARCHAR(128) NOT NULL,
    customer_name VARCHAR(128) DEFAULT '',
    question TEXT NOT NULL,
    status VARCHAR(16) DEFAULT 'pending',  -- pending / resolved
    created_at TIMESTAMP DEFAULT NOW(),
    resolved_at TIMESTAMP
);
