# Databricks notebook source
# /// script
# [tool.databricks.environment]
# environment_version = "5"
# ///
# MAGIC %md
# MAGIC # Ingest Weather Alerts -> Vector Embeddings (Lakebase)
# MAGIC
# MAGIC This notebook is part of the **Context Engineering on Databricks** course.
# MAGIC
# MAGIC It:
# MAGIC 1. Reads the weather documents that need embedding from Lakebase
# MAGIC 2. Computes sentence embeddings for the full narrative text (document-level)
# MAGIC 3. Stores the embeddings in the `weather_embeddings` table
# MAGIC 4. Chunks the narrative text into overlapping segments
# MAGIC 5. Computes embeddings for each chunk
# MAGIC 6. Stores chunk embeddings in the `weather_chunk_embeddings` table for more granular search
# MAGIC
# MAGIC The result is a vector database that supports both coarse (document) and fine-grained (chunk) semantic search over weather event narratives.

# COMMAND ----------

# DBTITLE 1,Install all required packages
# MAGIC %pip uninstall -y psycopg2 psycopg2-binary
# MAGIC %pip install -q 'databricks-sdk>=0.118.0' 'sentence-transformers>=2.3.0' trafilatura requests pandas

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Config
# MAGIC %md
# MAGIC ## Config
# MAGIC
# MAGIC Widgets let you override the source/destination table names and the embedding model without editing the notebook - useful when running this as a scheduled Databricks Job.

# COMMAND ----------

dbutils.widgets.text("documents_table_name", "weather_documents", "Source table (weather docs)")
dbutils.widgets.text("embeddings_table_name", "weather_embeddings", "Destination table (vectors)")
dbutils.widgets.text("chunk_embeddings_table_name", "weather_chunk_embeddings", "Destination table (chunk vectors)")
dbutils.widgets.text("embedding_model", "sentence-transformers/all-MiniLM-L12-v2", "Embedding model")

DOCUMENTS_TABLE_NAME = dbutils.widgets.get("documents_table_name")
EMBEDDINGS_TABLE_NAME = dbutils.widgets.get("embeddings_table_name")
CHUNK_EMBEDDINGS_TABLE_NAME = dbutils.widgets.get("chunk_embeddings_table_name")
EMBEDDING_MODEL_NAME = dbutils.widgets.get("embedding_model")
EMBEDDING_DIM = 384  # all-MiniLM-L12-v2 produces 384-dimensional vectors

print(f"Using model '{EMBEDDING_MODEL_NAME}' -> {EMBEDDING_DIM}-dim vectors")

# COMMAND ----------

# DBTITLE 1,Resolve the Lakebase connection URL
# MAGIC %md
# MAGIC ## Resolve the Lakebase connection URL
# MAGIC
# MAGIC Same secret, same decoding scheme as `lakebase.py`: a single base64-encoded Postgres URL (`postgresql://role:password@host:5432/db?sslmode=require`) stored in a Databricks secret scope. We parse it into the pieces psycopg2 needs for connection (host/port/dbname/user/password).

# COMMAND ----------

# DBTITLE 1,Parse Lakebase Connection Info
import base64
from urllib.parse import urlparse

from databricks.sdk import WorkspaceClient

w = WorkspaceClient()

def get_lakebase_url() -> str:
    secret = w.secrets.get_secret(scope="data_base", key="lakebase_url_day2")
    return base64.b64decode(secret.value).decode("utf-8")

lakebase_url = get_lakebase_url()
parsed = urlparse(lakebase_url)

# Extract connection details directly from the secret
db_host = parsed.hostname
db_port = parsed.port or 5432
db_name = parsed.path.lstrip('/')
db_user = parsed.username
db_password = parsed.password

print("Connection details:")
print(f"  Host: {db_host}:{db_port}")
print(f"  Database: {db_name}")
print(f"  User: {db_user}")
print(f"  Using raw credentials from secret (no OAuth)")

# COMMAND ----------

# DBTITLE 1,Test Psycopg2 connection
import psycopg2

print(f"Testing connection to {db_host}:{db_port}/{db_name}")
print(f"Using credentials as user: {db_user}\n")

try:
    conn = psycopg2.connect(
        host=db_host,
        port=db_port,
        dbname=db_name,
        user=db_user,
        password=db_password,
        sslmode='require'
    )
    
    cursor = conn.cursor()
    cursor.execute(f"SELECT COUNT(*) FROM {DOCUMENTS_TABLE_NAME}")
    count = cursor.fetchone()[0]
    
    # Get sample columns
    cursor.execute(f"""
        SELECT column_name 
        FROM information_schema.columns 
        WHERE table_name = '{DOCUMENTS_TABLE_NAME}' 
        ORDER BY ordinal_position 
        LIMIT 5
    """)
    columns = [row[0] for row in cursor.fetchall()]
    
    cursor.close()
    conn.close()
    
    print(f"✅ Connection successful! Found {count} rows in {DOCUMENTS_TABLE_NAME}")
    print(f"\nFirst 5 columns: {columns}")
    
except Exception as e:
    print(f"❌ Connection failed: {e}")

# COMMAND ----------

# DBTITLE 1,Load raw weather documents
# MAGIC %md
# MAGIC ## Load raw weather documents
# MAGIC
# MAGIC Reads the whole `weather_documents` table using psycopg2 into a pandas DataFrame for embedding computation.

# COMMAND ----------

import pandas as pd
import psycopg2

# Load weather documents using psycopg2
conn = psycopg2.connect(
    host=db_host,
    port=db_port,
    dbname=db_name,
    user=db_user,
    password=db_password,
    sslmode='require'
)

try:
    # Query with embedding_text computed
    query = f"""
        SELECT 
            id,
            location,
            source_type,
            headline,
            TRIM(COALESCE(narrative_text, '')) AS embedding_text
        FROM {DOCUMENTS_TABLE_NAME}
        WHERE narrative_text IS NOT NULL
          AND TRIM(narrative_text) != ''
    """
    
    weather_df = pd.read_sql_query(query, conn)
    print(f"Loaded {len(weather_df)} weather documents from {DOCUMENTS_TABLE_NAME}")
    display(weather_df.head(5))
finally:
    conn.close()

# COMMAND ----------

# DBTITLE 1,Compute embeddings
# MAGIC %md
# MAGIC ## Compute embeddings
# MAGIC
# MAGIC Loads the sentence-transformers model once and applies it in batches to the weather documents.

# COMMAND ----------

import os
import pandas as pd
from sentence_transformers import SentenceTransformer

# Set up HuggingFace cache
os.environ["HF_HOME"] = "/tmp/.cache/huggingface"
os.environ["TRANSFORMERS_CACHE"] = "/tmp/.cache/huggingface"
os.environ["HF_HUB_CACHE"] = "/tmp/.cache/huggingface"

print(f"Loading embedding model {EMBEDDING_MODEL_NAME}...")
model = SentenceTransformer(EMBEDDING_MODEL_NAME, cache_folder="/tmp/.cache/huggingface")

# Compute embeddings in batches for memory efficiency
print("Computing embeddings...")
batch_size = 32
all_embeddings = []

for i in range(0, len(weather_df), batch_size):
    batch = weather_df.iloc[i:i+batch_size]
    vectors = model.encode(batch["embedding_text"].tolist(), show_progress_bar=False)
    all_embeddings.extend(vectors.tolist())
    if (i + batch_size) % 128 == 0:
        print(f"  Processed {min(i + batch_size, len(weather_df))}/{len(weather_df)} documents")

# Create embeddings DataFrame
embeddings_df = pd.DataFrame({
    "id": weather_df["id"],
    "location": weather_df["location"],
    "source_type": weather_df["source_type"],
    "headline": weather_df["headline"],
    "embedding_text": weather_df["embedding_text"],
    "embedding": all_embeddings,
})

print(f"Computed {len(embeddings_df)} embeddings using {EMBEDDING_MODEL_NAME}")

# COMMAND ----------

# DBTITLE 1,Ensure the pgvector destination table exists
# MAGIC %md
# MAGIC ## Ensure the pgvector destination table exists
# MAGIC
# MAGIC The `pgvector` extension must be enabled and the destination table created with the correct vector dimension before inserting embeddings.

# COMMAND ----------

# Before running the cells below, ensure you've manually run:
#   create_tables.sql
# This file includes the weather_embeddings table setup.
print(f"Required EMBEDDING_DIM for SQL setup: {EMBEDDING_DIM}")
print(f"Table name: {EMBEDDINGS_TABLE_NAME}")
print("\nRun create_tables.sql in your Lakebase database before continuing.")

# COMMAND ----------

# DBTITLE 1,Upsert embeddings into Lakebase
# MAGIC %md
# MAGIC ## Upsert embeddings into Lakebase
# MAGIC
# MAGIC Written in batches via psycopg2's `executemany` for throughput. Each embedding is cast to Postgres' `vector` type via `::vector`.

# COMMAND ----------

# DBTITLE 1,Insert embeddings using psycopg2
import psycopg2
from psycopg2.extras import execute_values
from datetime import datetime

# Add model_name and created_at columns
embeddings_df['model_name'] = EMBEDDING_MODEL_NAME
embeddings_df['created_at'] = datetime.now()

embeddings_rows = embeddings_df.to_dict('records')

if len(embeddings_rows) > 0:
    print(f"Inserting {len(embeddings_rows)} embeddings into {EMBEDDINGS_TABLE_NAME}...")
    
    # Build connection from parsed URL
    conn = psycopg2.connect(
        host=db_host,
        port=db_port,
        dbname=db_name,
        user=db_user,
        password=db_password,
        sslmode='require'
    )
    
    try:
        cursor = conn.cursor()
        
        # Prepare data tuples for batch insert
        # Format embedding as PostgreSQL array literal: '{val1,val2,...}'
        insert_data = [
            (
                row['id'],
                0,  # chunk_index (0 for full document)
                row['embedding_text'] if 'embedding_text' in row else '',
                row['model_name'],
                '{' + ','.join(str(float(x)) for x in row['embedding']) + '}'
            )
            for row in embeddings_rows
        ]
        
        # Batch insert with ON CONFLICT DO NOTHING for deduplication
        insert_sql = f"""
            INSERT INTO {EMBEDDINGS_TABLE_NAME} (
                document_id, chunk_index, chunk_text, model_name, embedding
            ) VALUES %s
            ON CONFLICT (document_id, chunk_index) DO NOTHING
        """
        
        # execute_values is much faster than individual INSERTs
        template = "(%s, %s, %s, %s, %s::double precision[])"
        execute_values(cursor, insert_sql, insert_data, template=template, page_size=100)
        
        conn.commit()
        inserted_count = cursor.rowcount
        print(f"✅ Successfully inserted {inserted_count} new embeddings")
        print(f"   (Duplicates were skipped via ON CONFLICT DO NOTHING)")
        
    finally:
        cursor.close()
        conn.close()
        
print("\nIMPORTANT: Run the next cell to cast arrays to vectors for search!")

# COMMAND ----------

# DBTITLE 1,Cast arrays to vectors (FIX for vector search)
# MAGIC %md
# MAGIC ## Cast arrays to vectors (FIX for vector search)
# MAGIC
# MAGIC psycopg2 inserts embeddings as `double precision[]` arrays. We must cast them to the `vector` type for pgvector's HNSW index to work.

# COMMAND ----------

import psycopg2

conn = psycopg2.connect(
    host=db_host,
    port=db_port,
    dbname=db_name,
    user=db_user,
    password=db_password,
    sslmode='require'
)

try:
    cursor = conn.cursor()
    
    print(f"Casting embeddings to vector type in {EMBEDDINGS_TABLE_NAME}...")
    cursor.execute(f"""
        UPDATE {EMBEDDINGS_TABLE_NAME} 
        SET embedding = embedding::vector 
        WHERE embedding IS NOT NULL
    """)
    
    conn.commit()
    rows_updated = cursor.rowcount
    print(f"✅ Cast {rows_updated} embeddings to vector type")
    
finally:
    cursor.close()
    conn.close()

# COMMAND ----------

# DBTITLE 1,Test vector search
# MAGIC %md
# MAGIC ## Test vector search
# MAGIC
# MAGIC Run a semantic search query to verify the embeddings work correctly.

# COMMAND ----------

import psycopg2
import numpy as np
from sentence_transformers import SentenceTransformer
# Test query
test_query = "tornado damage in residential areas"
print(f"Test query: '{test_query}'")


# Test vector search to verify it's working
print("Testing vector search...\n")

# Load the model
model = SentenceTransformer(EMBEDDING_MODEL_NAME, cache_folder="/tmp/.cache/huggingface")
# Compute embedding for test query
query_vector = model.encode([test_query])[0]
query_vector_str = '[' + ','.join(str(float(x)) for x in query_vector) + ']'

conn = psycopg2.connect(
    host=db_host,
    port=db_port,
    dbname=db_name,
    user=db_user,
    password=db_password,
    sslmode='require'
)

try:
    cursor = conn.cursor()
    
    # Semantic search using cosine similarity
    cursor.execute(f"""
        SELECT 
            wd.id,
            wd.location,
            wd.source_type,
            wd.headline,
            SUBSTRING(wd.narrative_text, 1, 200) as preview,
            1 - (we.embedding <=> %s::vector) as similarity
        FROM {EMBEDDINGS_TABLE_NAME} we
        JOIN {DOCUMENTS_TABLE_NAME} wd ON we.document_id = wd.id
        ORDER BY we.embedding <=> %s::vector
        LIMIT 5
    """, (query_vector_str, query_vector_str))
    
    results = cursor.fetchall()
    
    print("\n✅ Top 5 similar weather events:\n")
    for i, (doc_id, location, source_type, headline, preview, similarity) in enumerate(results, 1):
        print(f"{i}. {headline}")
        print(f"   Location: {location} | Type: {source_type}")
        print(f"   Similarity: {similarity:.4f}")
        print(f"   Preview: {preview}...\n")
        
finally:
    cursor.close()
    conn.close()

# COMMAND ----------

# DBTITLE 1,Fetch and chunk weather content
# MAGIC %md
# MAGIC ## Fetch and chunk weather content
# MAGIC
# MAGIC For more granular search, split each document into overlapping text chunks.

# COMMAND ----------

CHUNK_SIZE = 800  # Characters per chunk
CHUNK_OVERLAP = 100  # Overlap between chunks

def chunk_text(text: str, doc_id: str, location: str) -> list:
    """Split text into overlapping chunks."""
    if len(text) <= CHUNK_SIZE:
        chunk_id = f"{doc_id}_0"
        return [(chunk_id, doc_id, location, 0, text)]
    
    chunks = []
    start = 0
    chunk_idx = 0
    
    while start < len(text):
        end = start + CHUNK_SIZE
        chunk = text[start:end]
        
        # Try to break at sentence or word boundary
        if end < len(text):
            last_period = chunk.rfind('.')
            last_space = chunk.rfind(' ')
            
            if last_period > CHUNK_SIZE * 0.5:
                chunk = chunk[:last_period + 1]
                end = start + last_period + 1
            elif last_space > CHUNK_SIZE * 0.5:
                chunk = chunk[:last_space]
                end = start + last_space
        
        chunk_id = f"{doc_id}_{chunk_idx}"
        chunks.append((chunk_id, doc_id, location, chunk_idx, chunk.strip()))
        chunk_idx += 1
        start = end - CHUNK_OVERLAP
        
        if start >= len(text):
            break
    
    return chunks

# Chunk all documents
print("Chunking weather documents...")
all_chunks = []

for _, row in weather_df.iterrows():
    chunks = chunk_text(row['embedding_text'], row['id'], row['location'])
    all_chunks.extend(chunks)

chunks_df = pd.DataFrame(
    all_chunks, 
    columns=['id', 'document_id', 'location', 'chunk_index', 'chunk_text']
)

print(f"Created {len(chunks_df)} chunks from {len(weather_df)} documents")
print(f"Average chunks per document: {len(chunks_df) / len(weather_df):.1f}")
display(chunks_df.head(5))

# COMMAND ----------

# DBTITLE 1,Compute chunk embeddings
# MAGIC %md
# MAGIC ## Compute chunk embeddings
# MAGIC
# MAGIC Generate embeddings for each text chunk.

# COMMAND ----------

print("Computing chunk embeddings...")
batch_size = 32
all_chunk_embeddings = []

for i in range(0, len(chunks_df), batch_size):
    batch = chunks_df.iloc[i:i+batch_size]
    vectors = model.encode(batch['chunk_text'].tolist(), show_progress_bar=False)
    all_chunk_embeddings.extend(vectors.tolist())
    
    if (i + batch_size) % 128 == 0:
        print(f"  Processed {min(i + batch_size, len(chunks_df))}/{len(chunks_df)} chunks")

# Add embeddings to chunks DataFrame
chunks_df['embedding'] = all_chunk_embeddings
chunks_df['model_name'] = EMBEDDING_MODEL_NAME
chunks_df['embedded_at'] = datetime.now()

print(f"\n✅ Computed {len(chunks_df)} chunk embeddings using {EMBEDDING_MODEL_NAME}")

# COMMAND ----------

# DBTITLE 1,Insert chunk embeddings into Lakebase
# MAGIC %md
# MAGIC ## Insert chunk embeddings into Lakebase
# MAGIC
# MAGIC Store the chunk embeddings in the `weather_chunk_embeddings` table.

# COMMAND ----------

# DBTITLE 1,Insert chunk embeddings using psycopg2
import psycopg2
from psycopg2.extras import execute_values

chunk_rows = chunks_df.to_dict('records')

if len(chunk_rows) > 0:
    print(f"Inserting {len(chunk_rows)} chunk embeddings into {CHUNK_EMBEDDINGS_TABLE_NAME}...")
    
    conn = psycopg2.connect(
        host=db_host,
        port=db_port,
        dbname=db_name,
        user=db_user,
        password=db_password,
        sslmode='require'
    )
    
    try:
        cursor = conn.cursor()
        
        # Prepare data tuples for batch insert
        insert_data = [
            (
                row['id'],
                row['document_id'],
                row['location'],
                int(row['chunk_index']),
                row['chunk_text'],
                '{' + ','.join(str(float(x)) for x in row['embedding']) + '}',
                row['model_name'],
                row['embedded_at']
            )
            for row in chunk_rows
        ]
        
        # Batch insert with ON CONFLICT DO NOTHING
        insert_sql = f"""
            INSERT INTO {CHUNK_EMBEDDINGS_TABLE_NAME} (
                id, document_id, location, chunk_index, chunk_text, 
                embedding, model_name, embedded_at
            ) VALUES %s
            ON CONFLICT (id) DO NOTHING
        """
        
        template = "(%s, %s, %s, %s, %s, %s::double precision[], %s, %s)"
        execute_values(cursor, insert_sql, insert_data, template=template, page_size=100)
        
        conn.commit()
        inserted_count = cursor.rowcount
        print(f"✅ Successfully inserted {inserted_count} new chunk embeddings")
        print(f"   (Duplicates were skipped via ON CONFLICT DO NOTHING)")
        
    finally:
        cursor.close()
        conn.close()
        
print("\nIMPORTANT: Run the next cell to cast chunk arrays to vectors!")

# COMMAND ----------

# DBTITLE 1,Cast chunk arrays to vectors (FIX for chunk search)
# MAGIC %md
# MAGIC ## Cast chunk arrays to vectors (FIX for chunk search)
# MAGIC
# MAGIC Cast the chunk embeddings from arrays to vector type for HNSW indexing.

# COMMAND ----------

import psycopg2

conn = psycopg2.connect(
    host=db_host,
    port=db_port,
    dbname=db_name,
    user=db_user,
    password=db_password,
    sslmode='require'
)

try:
    cursor = conn.cursor()
    
    print(f"Casting chunk embeddings to vector type in {CHUNK_EMBEDDINGS_TABLE_NAME}...")
    cursor.execute(f"""
        UPDATE {CHUNK_EMBEDDINGS_TABLE_NAME} 
        SET embedding = embedding::vector 
        WHERE embedding IS NOT NULL
    """)
    
    conn.commit()
    rows_updated = cursor.rowcount
    print(f"✅ Cast {rows_updated} chunk embeddings to vector type")
    
    # Verify the table
    cursor.execute(f"SELECT COUNT(*) FROM {CHUNK_EMBEDDINGS_TABLE_NAME}")
    total_chunks = cursor.fetchone()[0]
    print(f"\nTotal chunks in table: {total_chunks}")
    
finally:
    cursor.close()
    conn.close()

print("\n" + "="*60)
print("✅ Embedding pipeline complete!")
print("="*60)
print(f"Documents embedded: {len(weather_df)}")
print(f"Chunks embedded: {len(chunks_df)}")
print("\nYou can now run semantic searches on weather events!")