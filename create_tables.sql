-- ============================================================================
-- Weather Intelligence RAG - Complete Database Schema
-- ============================================================================
-- Target: Lakebase Postgres Database
-- Extension: pgvector (for semantic search)
-- Tables: weather_documents (raw data) + weather_embeddings (vectors)
-- ============================================================================


-- ============================================================================
-- STEP 1: Enable pgvector Extension
-- ============================================================================
-- This adds vector data type support for semantic search

CREATE EXTENSION IF NOT EXISTS vector;


-- ============================================================================
-- STEP 2: Create weather_documents Table
-- ============================================================================
-- Stores raw weather alerts and forecasts from NWS API

CREATE TABLE IF NOT EXISTS weather_documents (
    -- Primary key: deterministic hash based on location + issued_at
    id TEXT PRIMARY KEY,
    
    -- Location information (coordinates or place name)
    location TEXT NOT NULL,
    
    -- Source type: 'alert' for weather alerts, 'forecast' for forecasts
    source_type TEXT NOT NULL CHECK (source_type IN ('alert', 'forecast')),
    
    -- Brief summary or alert title
    headline TEXT,
    
    -- Full description/instruction text (main content)
    narrative_text TEXT NOT NULL,
    
    -- Timestamp when weather info was issued by NWS
    issued_at TIMESTAMP NOT NULL,
    
    -- Timestamp when we ingested this document
    synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    
    -- Full JSON response from NWS API (for reference/debugging)
    payload JSONB
);

-- Indexes for efficient querying on weather_documents
CREATE INDEX IF NOT EXISTS idx_weather_docs_location 
    ON weather_documents(location);

CREATE INDEX IF NOT EXISTS idx_weather_docs_source_type 
    ON weather_documents(source_type);

CREATE INDEX IF NOT EXISTS idx_weather_docs_issued_at 
    ON weather_documents(issued_at DESC);

-- GIN index for flexible JSONB queries on the payload
CREATE INDEX IF NOT EXISTS idx_weather_docs_payload 
    ON weather_documents USING GIN(payload);


-- ============================================================================
-- STEP 3: Create weather_embeddings Table (Full Document Embeddings)
-- ============================================================================
-- Stores full document embeddings (one embedding per document)
-- Uses sentence-transformers/all-MiniLM-L12-v2 (384 dimensions)

CREATE TABLE IF NOT EXISTS weather_embeddings (
    -- Auto-incrementing primary key
    id SERIAL PRIMARY KEY,
    
    -- Foreign key to weather_documents (cascading delete)
    document_id TEXT NOT NULL REFERENCES weather_documents(id) ON DELETE CASCADE,
    
    -- Position of this chunk within the document (0-indexed)
    chunk_index INTEGER NOT NULL,
    
    -- The actual text chunk being embedded
    chunk_text TEXT NOT NULL,
    
    -- Embedding model used (for tracking/versioning)
    model_name TEXT NOT NULL DEFAULT 'sentence-transformers/all-MiniLM-L12-v2',
    
    -- 384-dimensional vector embedding (pgvector type)
    embedding vector(384) NOT NULL,
    
    -- Timestamp when this embedding was created
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    
    -- Prevent duplicate chunks for the same document
    UNIQUE(document_id, chunk_index)
);

-- Index on document_id for efficient joins with weather_documents
CREATE INDEX IF NOT EXISTS idx_weather_embeddings_doc_id 
    ON weather_embeddings(document_id);

-- HNSW index for fast approximate nearest neighbor search
-- Uses cosine similarity for semantic search
-- This is the KEY index that makes semantic search fast!
CREATE INDEX IF NOT EXISTS idx_weather_embeddings_hnsw 
    ON weather_embeddings 
    USING hnsw (embedding vector_cosine_ops);


-- ============================================================================
-- STEP 4: Create weather_chunk_embeddings Table (Chunked Document Embeddings)
-- ============================================================================
-- Stores text chunks with vector embeddings for semantic search
-- Allows for larger documents to be split and searched more granularly
-- Uses sentence-transformers/all-MiniLM-L12-v2 (384 dimensions)

CREATE TABLE IF NOT EXISTS weather_chunk_embeddings (
    -- Unique identifier for each chunk (composite key: document_id + chunk_index)
    id TEXT PRIMARY KEY,
    
    -- Foreign key to weather_documents (reference only, not enforced for flexibility)
    document_id TEXT NOT NULL,
    
    -- Location information (denormalized for faster queries)
    location TEXT NOT NULL,
    
    -- Position of this chunk within the document (0-indexed)
    chunk_index INTEGER NOT NULL,
    
    -- The actual text chunk being embedded
    chunk_text TEXT NOT NULL,
    
    -- 384-dimensional vector embedding (pgvector type)
    embedding VECTOR(384) NOT NULL,
    
    -- Embedding model used (for tracking/versioning)
    model_name TEXT NOT NULL,
    
    -- Timestamp when this embedding was created
    embedded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Index on document_id for efficient joins with weather_documents
CREATE INDEX IF NOT EXISTS idx_weather_chunk_embeddings_doc_id 
    ON weather_chunk_embeddings(document_id);

-- Index on location for location-based filtering
CREATE INDEX IF NOT EXISTS idx_weather_chunk_embeddings_location 
    ON weather_chunk_embeddings(location);

-- HNSW index for fast approximate nearest neighbor search
-- Uses cosine similarity for semantic search
CREATE INDEX IF NOT EXISTS idx_weather_chunk_embeddings_hnsw 
    ON weather_chunk_embeddings 
    USING hnsw (embedding vector_cosine_ops);


-- ============================================================================
-- Verification Queries
-- ============================================================================
-- Run these after creating tables to verify everything is set up correctly

-- 1. Check pgvector extension is installed
SELECT extname, extversion 
FROM pg_extension 
WHERE extname = 'vector';

-- 2. Check tables exist
SELECT table_name, 
       (SELECT COUNT(*) 
        FROM information_schema.columns 
        WHERE table_name = t.table_name) as column_count
FROM information_schema.tables t
WHERE table_schema = 'public' 
  AND table_name IN ('weather_documents', 'weather_embeddings', 'weather_chunk_embeddings')
ORDER BY table_name;

-- 3. List all indexes
SELECT tablename, 
       indexname, 
       indexdef
FROM pg_indexes 
WHERE schemaname = 'public' 
  AND tablename IN ('weather_documents', 'weather_embeddings', 'weather_chunk_embeddings')
ORDER BY tablename, indexname;

-- 4. Count rows (should be 0 initially)
SELECT 
    (SELECT COUNT(*) FROM weather_documents) as document_count,
    (SELECT COUNT(*) FROM weather_embeddings) as embedding_count,
    (SELECT COUNT(*) FROM weather_chunk_embeddings) as chunk_embedding_count;

-- 5. Describe table structures
-- weather_documents columns
SELECT column_name, data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_name = 'weather_documents'
ORDER BY ordinal_position;

-- weather_embeddings columns
SELECT column_name, data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_name = 'weather_embeddings'
ORDER BY ordinal_position;

-- weather_chunk_embeddings columns
SELECT column_name, data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_name = 'weather_chunk_embeddings'
ORDER BY ordinal_position;


-- ============================================================================
-- Sample Queries (for testing after data ingestion)
-- ============================================================================

-- Get most recent weather documents
-- SELECT id, location, source_type, headline, issued_at 
-- FROM weather_documents 
-- ORDER BY issued_at DESC 
-- LIMIT 10;

-- Count documents by type
-- SELECT source_type, COUNT(*) 
-- FROM weather_documents 
-- GROUP BY source_type;

-- Semantic search example (after embeddings are created)
-- SELECT 
--     e.document_id,
--     d.headline,
--     d.location,
--     e.chunk_text,
--     (e.embedding <=> '[0.1, 0.2, ...]'::vector) as distance
-- FROM weather_embeddings e
-- JOIN weather_documents d ON e.document_id = d.id
-- ORDER BY distance
-- LIMIT 5;
