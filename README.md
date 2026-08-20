# Weather Intelligence RAG Service

A production-ready Retrieval-Augmented Generation (RAG) service for semantic search over weather data using pgvector and sentence-transformers.

## Architecture Overview

### System Components

```
┌─────────────────────────────────────┐
│            USER                     │
└────────┬─────────────────────────────┘
         │                      │
    Web UI (HTML/CSS/JS)    REST API Clients
         │                      │
         └────────┬────────────┘
                  │
         ┌────────┴─────────┐
         │  Flask REST API  │
         └────────┬─────────┘
                  │
      ┌───────────┼───────────────────┐
      │            │                    │
      │            │                    │
   NWS API      Embeddings            Database
   Client      Generator             Manager
      │            │                    │
      ↓            ↓                    ↓
  National   sentence-        Lakebase PostgreSQL
  Weather    transformers     with pgvector
  Service        │                    │
                 │                    │
                 │        ┌──────────┴──────────┐
                 │        │                       │
                 │   weather_documents    weather_embeddings
                 │   (raw data)          (vector(384))
                 │        │                       │
                 └────────┴───────────────────────┘
                           │
                   Cosine Similarity Search
                    (IVFFlat Index)
```

### Technology Stack

* **Database**: Lakebase PostgreSQL with pgvector extension
* **Embeddings**: sentence-transformers/all-MiniLM-L6-v2 (384 dimensions)
* **Vector Search**: IVFFlat index for cosine similarity
* **Web Framework**: Flask with Jinja2 templates
* **Frontend**: HTML5, CSS3 (custom responsive design), vanilla JavaScript
* **Data Source**: National Weather Service (NWS) public API

### UI/UX Features

* **Responsive Design**: Works on desktop and mobile devices
* **Vertical Layout**: Sync and search sections stack vertically for better readability
* **Visual Feedback**: 
  * Real-time loading spinners during operations
  * Color-coded success/error messages
  * Animated similarity score bars (gradient-filled)
  * Hover effects on interactive elements
* **Modern Aesthetics**:
  * Gradient backgrounds (purple/blue theme)
  * Card-based layout with shadows and transitions
  * Icon-based navigation
  * Clean typography with proper spacing
* **Accessibility**: Form labels, semantic HTML, focus states

---

## Database Schema

### Table: `weather_documents`

Stores raw weather alerts and forecasts.

| Column | Type | Description |
|--------|------|-------------|
| `id` | TEXT PRIMARY KEY | Deterministic hash (location + issued_at + source_type) |
| `location` | TEXT | Location string (e.g., "Chicago, IL" or "41.88,-87.63") |
| `source_type` | TEXT | Either 'alert' or 'forecast' |
| `headline` | TEXT | Brief summary or alert title |
| `narrative_text` | TEXT | Full description/detailed forecast |
| `issued_at` | TIMESTAMP | When weather info was issued |
| `synced_at` | TIMESTAMP | When record was ingested |
| `payload` | JSONB | Full JSON response from NWS API |

**Indexes**: location, source_type, issued_at, payload (GIN)

### Table: `weather_embeddings`

Stores vector embeddings for semantic search.

| Column | Type | Description |
|--------|------|-------------|
| `id` | SERIAL PRIMARY KEY | Auto-incrementing ID |
| `document_id` | TEXT | Foreign key to weather_documents(id) |
| `chunk_text` | TEXT | The actual text chunk |
| `embedding` | vector(384) | 384-dimensional embedding vector |
| `created_at` | TIMESTAMP | When embedding was created |

**Indexes**: 
* document_id (B-tree)
* embedding (IVFFlat with vector_cosine_ops, 100 lists)

**Foreign Key**: `ON DELETE CASCADE` - deleting a document removes all embeddings

---

## API Endpoints

### 1. Web Interface

```http
GET /
```

Serves the interactive web UI with two main sections displayed vertically:
* **Sync Weather Data** - Form to fetch and sync weather data from NWS API
* **Vector Search** - Semantic search interface with natural language queries

### 2. Health Check

```http
GET /api/health
```

**Response:**
```json
{
  "status": "healthy",
  "service": "Weather Intelligence RAG Service",
  "version": "1.0.0",
  "endpoints": [
    "POST /weather/sync - Sync weather data from NWS API",
    "POST /weather/search - Semantic search over weather documents",
    "GET /weather/stats - Get database statistics",
    "GET /weather/documents - List synced weather documents",
    "DELETE /weather/documents/<id> - Delete a specific document",
    "POST /weather/documents/cleanup - Delete old documents",
    "GET /health/database - Check database connectivity"
  ]
}
```

### 3. Sync Weather Data

```http
POST /weather/sync
```

**Request Body:**
```json
{
  "locations": ["41.88,-87.63", "IL", "30.27,-97.74"],
  "limit": 10
}
```

**Parameters:**
* `locations` (array, required): List of locations
  * Format: `"lat,lon"` for coordinates (e.g., `"41.88,-87.63"`)
  * Format: `"XX"` for state codes (e.g., `"IL"` for Illinois alerts)
* `limit` (integer, optional): Max items per location (1-50, default: 10)

**Response:**
```json
{
  "status": "success",
  "statistics": {
    "alerts": 5,
    "forecasts": 14,
    "upserted": 19,
    "errors": 0
  },
  "message": "Synced 19 documents"
}
```

**Behavior:**
* Fetches alerts via NWS `/alerts/active?area={state}`
* Fetches forecasts via NWS `/gridpoints/{office}/{x},{y}/forecast`
* Uses `INSERT ... ON CONFLICT` for idempotent upserts
* Updates existing documents if location+timestamp already exists

### 4. Semantic Search

```http
POST /weather/search
```

**Request Body:**
```json
{
  "query": "flash flood risk this weekend",
  "top_k": 5
}
```

**Parameters:**
* `query` (string, required): Natural language search query
* `top_k` (integer, optional): Number of results to return (1-20, default: 5)

**Response:**
```json
{
  "status": "success",
  "query": "flash flood risk this weekend",
  "top_k": 5,
  "count": 5,
  "results": [
    {
      "location": "Chicago, IL",
      "source_type": "alert",
      "headline": "Flash Flood Warning",
      "chunk_text": "...The National Weather Service has issued a Flash Flood Warning...",
      "issued_at": "2024-01-15T10:30:00Z",
      "similarity_score": 0.8723
    }
  ]
}
```

**Behavior:**
* Vectorizes query using all-MiniLM-L6-v2
* Executes pgvector cosine similarity search
* Returns top_k chunks ranked by similarity (1 - cosine distance)
* Joins with `weather_documents` for metadata

### 5. Database Statistics

```http
GET /weather/stats
```

**Response:**
```json
{
  "status": "success",
  "statistics": {
    "total_documents": 150,
    "alerts": 45,
    "forecasts": 105,
    "total_embeddings": 1200,
    "latest_sync": "2024-01-15T10:35:00Z"
  }
}
```

### 6. List Weather Documents

```http
GET /weather/documents?limit=100&source_type=alert
```

**Query Parameters:**
* `limit` (integer, optional): Maximum number of documents (default: 100, max: 500)
* `source_type` (string, optional): Filter by 'alert' or 'forecast'

**Response:**
```json
{
  "status": "success",
  "count": 25,
  "documents": [
    {
      "id": "abc123...",
      "location": "Chicago, IL",
      "source_type": "alert",
      "headline": "Flash Flood Warning",
      "issued_at": "2024-01-15T10:30:00Z",
      "synced_at": "2024-01-15T10:35:00Z"
    }
  ]
}
```

### 7. Delete Weather Document

```http
DELETE /weather/documents/<document_id>
```

**Response:**
```json
{
  "status": "success",
  "deleted": true,
  "document_id": "abc123..."
}
```

### 8. Cleanup Old Documents

```http
POST /weather/documents/cleanup
```

**Request Body:**
```json
{
  "days_old": 30
}
```

**Response:**
```json
{
  "status": "success",
  "deleted_count": 45,
  "message": "Deleted 45 documents older than 30 days"
}
```

### 9. Database Health

```http
GET /health/database
```

**Response:**
```json
{
  "status": "healthy",
  "database_connected": true,
  "pgvector_enabled": true
}
```

---

## Setup & Execution

### Prerequisites

1. **Lakebase PostgreSQL** with pgvector extension enabled
2. **Python 3.9+**
3. **Databricks workspace** (for deployment)

### Step 1: Database Initialization

**Option A: Automated Setup (Recommended)**

Use the provided setup script to initialize your database automatically:

```bash
# Set environment variables for your Lakebase instance
export LAKEBASE_HOST="<your-instance>.cloud.databricks.com"
export LAKEBASE_PORT="5432"
export LAKEBASE_DATABASE="weather-retrieval-service"
export LAKEBASE_USER="<your-username>"
export LAKEBASE_PASSWORD="<your-password>"

# Run the automated setup script
python setup_database.py
```

The script will:
* Test database connectivity
* Create the pgvector extension
* Create all tables (weather_documents, weather_embeddings)
* Create all indexes (B-tree, GIN, HNSW)
* Verify the setup
* Display a summary with table counts and database size

**Expected Output:**
```
######################################################################
#                                                                    #
#  Weather Intelligence RAG - Database Setup                         #
#                                                                    #
######################################################################

======================================================================
  Testing Database Connection
======================================================================
✓ Connected to PostgreSQL
ℹ Version: PostgreSQL 14.x

======================================================================
  Creating pgvector Extension
======================================================================
✓ Created pgvector extension
✓ pgvector version: 0.5.0

======================================================================
  Executing Schema Definition
======================================================================
✓ Executed: CREATE EXTENSION
✓ Executed: CREATE TABLE
✓ Executed: CREATE TABLE
✓ Executed: CREATE INDEX
...

======================================================================
  Verifying Tables
======================================================================
✓ Table 'weather_documents' exists (8 columns)
✓ Table 'weather_embeddings' exists (7 columns)

======================================================================
  Verifying Indexes
======================================================================
✓ Index 'idx_weather_docs_location' exists (B-tree)
✓ Index 'idx_weather_embeddings_hnsw' exists (HNSW (vector))
...

======================================================================
  Database Summary
======================================================================
ℹ Documents in weather_documents: 0
ℹ Embeddings in weather_embeddings: 0
ℹ Database size: 8192 kB

======================================================================
  ✓ DATABASE SETUP COMPLETE
======================================================================

Next steps:
  1. Sync weather data: python app.py (then POST /weather/sync)
  2. Generate embeddings: python ingest_weather_embeddings.py
  3. Test search: POST /weather/search with a query
```

**Option B: Manual Setup**

If you prefer to run SQL directly:

```bash
# Connect to database and run schema
psql -h $LAKEBASE_HOST -p $LAKEBASE_PORT -U $LAKEBASE_USER -d $LAKEBASE_DATABASE < schema.sql
```

**Verify pgvector:**
```sql
SELECT * FROM pg_extension WHERE extname = 'vector';
```

### Step 2: Install Dependencies

```bash
pip install -r requirements.txt
```

**Note**: The sentence-transformers model (~500MB) is downloaded on first use (lazy-loaded when the first search is performed, not at app startup).

### Step 3: Sync Weather Data

```bash
# Option A: Via API (once Flask is running)
curl -X POST http://localhost:8080/weather/sync \
  -H "Content-Type: application/json" \
  -d '{
    "locations": ["41.88,-87.63", "IL", "30.27,-97.74"],
    "limit": 10
  }'

# Option B: Via Python client directly
python -c "
import weather_client
client = weather_client.NWSClient()
stats = client.sync_weather_data(['41.88,-87.63', 'IL'], limit=10)
print(stats)
"
```

### Step 4: Generate Embeddings

```bash
python ingest_weather_embeddings.py --limit 100
```

**Output:**
```
Loading embedding model: sentence-transformers/all-MiniLM-L6-v2...
✓ Model loaded. Embedding dimension: 384
Querying unprocessed documents...
Found 19 documents to process
Embedding 3 chunks for document 5f3a2b...
Embedding 5 chunks for document 8c7d1e...
...
Inserting 47 embeddings into database...
✓ Inserted 47 embeddings

Processing Complete
Documents processed: 19
Text chunks created: 47
Embeddings stored: 47
```

### Step 5: Start Flask API

```bash
python app.py
```

**Output:**
```
Loading sentence-transformers model at startup...
✓ Model loaded: sentence-transformers/all-MiniLM-L6-v2

Verifying database connectivity...
✓ Database connection successful
✓ pgvector extension enabled

Starting Weather RAG Service on port 8080...
```

### Step 6: Test Semantic Search

```bash
curl -X POST http://localhost:8080/weather/search \
  -H "Content-Type: application/json" \
  -d '{
    "query": "severe thunderstorm with damaging winds",
    "top_k": 3
  }'
```

---

## Databricks Free Edition Optimizations

### Memory Management

* **Batch Size**: 50-100 chunks per embedding generation
* **Model Loading**: Preload sentence-transformers at app startup (not per request)
* **Connection Pooling**: Close psycopg2 connections immediately after use

### CPU-Only Embeddings

```python
# sentence-transformers automatically uses CPU
model = SentenceTransformer("all-MiniLM-L6-v2")
embeddings = model.encode(texts, batch_size=50)
```

### HNSW Index Benefits

* **Fast nearest neighbor search** even on low-memory nodes
* **Approximate search** with high recall (>95%)
* **Lower memory footprint** than brute-force exact search

---

## Text Chunking Strategy

**Parameters:**
* Chunk size: 800 characters
* Overlap: 100 characters

**Benefits:**
* Prevents context loss across chunk boundaries
* Short alerts remain as single chunks
* Long forecasts split into coherent passages

**Algorithm:**
```python
def chunk_text(text, size=800, overlap=100):
    chunks = []
    start = 0
    while start < len(text):
        end = start + size
        chunk = text[start:end]
        
        # Break at sentence boundary if possible
        if end < len(text):
            last_period = chunk.rfind('.')
            if last_period > size * 0.5:
                end = start + last_period + 1
        
        chunks.append(chunk.strip())
        start = end - overlap
    
    return chunks
```

---

## Model Selection Rationale

**Chosen Model**: `sentence-transformers/all-MiniLM-L6-v2`

**Why?**
* **Dimension**: 384 (smaller than all-mpnet-base-v2's 768)
* **Quality**: Strong performance on semantic similarity tasks
* **Speed**: Fast inference on CPU (~100 sentences/sec)
* **Size**: 80MB download vs 420MB for larger models
* **Compatibility**: Native pgvector support for fixed-dimension vectors

**Trade-offs:**
* Slightly lower accuracy than larger models
* Optimized for English text
* Good balance for Free Edition constraints

---

## Troubleshooting

### pgvector Not Found

```sql
CREATE EXTENSION vector;
```

### Connection Pool Exhausted

Use context manager pattern:
```python
with get_db_connection() as conn:
    # ... do work
    pass  # Connection auto-closes
```

### Out of Memory During Embedding

Reduce batch size in `ingest_weather_embeddings.py`:
```python
BATCH_SIZE = 25  # Down from 50
```

### No Results from Search

Check if embeddings exist:
```sql
SELECT COUNT(*) FROM weather_embeddings;
```

### Slow Search Queries

Verify IVFFlat index exists:
```sql
\di idx_weather_emb_vector
```

Or check all indexes:
```sql
SELECT indexname, indexdef 
FROM pg_indexes 
WHERE tablename = 'weather_embeddings';
```

### Web UI Not Loading

Check Flask is serving templates correctly:
```bash
# Verify templates directory exists
ls -la templates/

# Should contain index.html
ls templates/index.html
```

### Search Returns Empty Results

1. Verify embeddings were generated:
   ```sql
   SELECT COUNT(*) FROM weather_embeddings;
   ```

2. Check if documents exist:
   ```sql
   SELECT COUNT(*) FROM weather_documents;
   ```

3. Run the embedding generation script if needed:
   ```bash
   python ingest_weather_embeddings.py --limit 100
   ```

---

## Future Enhancements

* [ ] Add geocoding service for City,State → lat,lon conversion
* [ ] Implement query result caching
* [ ] Add filtering by date range, severity, source_type
* [ ] Support hybrid search (keyword + semantic)
* [ ] Add authentication/rate limiting
* [ ] Implement streaming embeddings for real-time updates
* [ ] Add Grafana dashboard for monitoring

---

## Configuration

### Environment Variables

The application uses the following environment variables for Lakebase PostgreSQL connection (configured in `lakebase.py`):

* `LAKEBASE_HOST` - Lakebase instance hostname (e.g., `<instance>.cloud.databricks.com`)
* `LAKEBASE_PORT` - PostgreSQL port (default: `5432`)
* `LAKEBASE_DATABASE` - Database name (e.g., `weather-retrieval-service`)
* `LAKEBASE_USER` - Database username
* `LAKEBASE_PASSWORD` - Database password (store in Databricks secrets)
* `PORT` - Flask server port (default: `8080`)

### Databricks Secrets Integration

For production deployments, credentials should be stored in Databricks secrets:

```python
from databricks.sdk import WorkspaceClient

w = WorkspaceClient()
db_password = w.secrets.get_secret(
    scope="lakebase-secrets",
    key="postgres-password"
).value
```

### File Structure

```
databricks-bootcamp-assignament-day2/
├── app.py                          # Main Flask application
├── lakebase.py                      # Database connection utilities
├── weather_client.py                # NWS API client
├── ingest_weather_embeddings.py     # Embedding generation script
├── setup_database.py                # Database initialization script
├── schema.sql                       # Database schema definition
├── requirements.txt                 # Python dependencies
├── README_WEATHER.md                # This file
├── templates/
│   └── index.html                   # Web UI template
└── static/
    ├── css/
    │   └── style.css                # Application styles
    └── js/
        └── main.js                   # Frontend JavaScript
```

---

## Deployment Options

### Option 1: Databricks Apps (Recommended)

Deploy as a Databricks App for production use:

```bash
# Initialize app.yaml
databricks apps create weather-rag-app

# Deploy
databricks apps deploy weather-rag-app
```

### Option 2: Local Development

Run locally for development and testing:

```bash
# Set environment variables
export LAKEBASE_HOST="<your-instance>.cloud.databricks.com"
export LAKEBASE_DATABASE="weather-retrieval-service"
# ... other env vars

# Run Flask
python app.py
```

### Option 3: Databricks Notebook

Run the Flask app from a Databricks notebook:

```python
%pip install -r requirements.txt
import app
app.app.run(host="0.0.0.0", port=8080)
```

---

## License & Attribution

**Weather Data**: [National Weather Service API](https://www.weather.gov/documentation/services-web-api) (public domain)

**Embedding Model**: [sentence-transformers/all-MiniLM-L6-v2](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2) (Apache 2.0)

---

## Contact

For questions or issues, refer to the Databricks documentation or open an issue in the project repository.
