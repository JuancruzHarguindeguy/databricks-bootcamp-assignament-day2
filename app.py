"""Weather Intelligence RAG Service - Flask REST API."""

import json
import logging
import os
import re
from typing import Dict, List

import requests
from databricks.sdk import WorkspaceClient
from flask import Flask, jsonify, render_template, request

import lakebase
from weather_client import NWSClient

# Initialize Flask app
app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("weather-app")

_w = WorkspaceClient()


# Table configuration (note: actual tables are weather_documents and weather_embeddings)
# These env vars are kept for backward compatibility but not currently used
ALERTS_TABLE_NAME = os.environ.get("ALERTS_TABLE_NAME", "weather_intelligence.alerts")
FORECASTS_TABLE_NAME = os.environ.get("FORECASTS_TABLE_NAME", "weather_intelligence.forecasts")
EMBEDDING_TABLE_NAME = os.environ.get("EMBEDDING_TABLE_NAME", "weather_intelligence.embeddings")
CHUNK_EMBEDING_TABLE_NAME = os.environ.get("CHUNK_EMBEDING_TABLE_NAME", "weather_intelligence.chunk_embeddings")
EMBEDDING_MODEL_NAME = os.environ.get("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")


# Table initialization functions

def ensure_alerts_table():
    """Create alerts table if it doesn't exist."""
    try:
        with lakebase.get_connection() as conn:
            cursor = conn.cursor()
            
            # Create table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS weather_documents (
                    id TEXT PRIMARY KEY,
                    location TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    headline TEXT,
                    narrative_text TEXT,
                    issued_at TIMESTAMP,
                    payload JSONB,
                    synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Try to create indexes (may fail if table exists and we don't own it)
            try:
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_weather_docs_source ON weather_documents(source_type)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_weather_docs_issued ON weather_documents(issued_at)")
            except Exception as idx_err:
                logger.warning(f"Could not create indexes (table may exist with different owner): {idx_err}")
            
            conn.commit()
            logger.info("weather_documents table ensured")
    except Exception as e:
        logger.error(f"Failed to ensure weather_documents table: {e}")
        raise

def ensure_embeddings_table():
    """Create embeddings table with pgvector support."""
    try:
        with lakebase.get_connection() as conn:
            cursor = conn.cursor()
            
            # Enable pgvector extension
            cursor.execute("CREATE EXTENSION IF NOT EXISTS vector")
            
            # Create table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS weather_embeddings (
                    id SERIAL PRIMARY KEY,
                    document_id TEXT REFERENCES weather_documents(id) ON DELETE CASCADE,
                    chunk_text TEXT NOT NULL,
                    embedding vector(384),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Try to create indexes (may fail if table exists and we don't own it)
            try:
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_weather_emb_doc ON weather_embeddings(document_id)")
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_weather_emb_vector ON weather_embeddings 
                    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)
                """)
            except Exception as idx_err:
                logger.warning(f"Could not create indexes (table may exist with different owner): {idx_err}")
            
            conn.commit()
            logger.info("weather_embeddings table ensured")
    except Exception as e:
        logger.error(f"Failed to ensure weather_embeddings table: {e}")
        raise


def _current_user_email() -> str:
    """
    Resolve the current user's email so the watchlist can be personalized.

    Databricks Apps inject the logged-in user's identity via the
    X-Forwarded-Email header on every request. Fall back to the Databricks
    SDK's current_user API for local development where that header isn't set.
    """
    header_email = request.headers.get("X-Forwarded-Email")
    if header_email:
        return header_email
    return _w.current_user.me().user_name

#-------------------------------------------------------------------------------------------------------------------------------------------
# Health Check Endpoints
# - /api/health: General API health (confirms service is running)
# - /health/database: Specifically tests database connectivity (used by monitoring)
#-------------------------------------------------------------------------------------------------------------------------------------------

@app.route("/health/database", methods=["GET"])
def database_health():
    """Check database connectivity and pgvector extension."""
    try:
        db_connected = test_connection()
        pgvector_enabled = False
        
        if db_connected:
            # Check if pgvector extension is available
            try:
                with lakebase.get_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT extname FROM pg_extension WHERE extname = 'vector'")
                    pgvector_enabled = cursor.fetchone() is not None
            except Exception as e:
                logger.warning(f"pgvector check failed: {e}")
        
        return jsonify({
            "status": "healthy" if (db_connected and pgvector_enabled) else "unhealthy",
            "database_connected": db_connected,
            "pgvector_enabled": pgvector_enabled
        }), 200 if (db_connected and pgvector_enabled) else 503
        
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500

#-------------------------------------------------------------------------------------------------------------------------------------------


@app.errorhandler(Exception)
def handle_exception(err):
    """Ensure all unhandled errors return JSON (not an HTML error page),
    so the frontend's resp.json() call never chokes on HTML."""
    logger.exception("Unhandled exception while processing request")
    status_code = getattr(err, "code", 500)
    if not isinstance(status_code, int):
        status_code = 500
    return jsonify({"error": str(err)}), status_code


#-------------------------------------------------------------------------------------------------------------------------------------------


# Database helper functions
def test_connection() -> bool:
    """Test database connectivity."""
    try:
        with lakebase.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            return True
    except Exception as e:
        logger.error(f"Database connection test failed: {e}")
        return False


def get_database_stats() -> dict:
    """Get statistics about stored weather data."""
    try:
        with lakebase.get_connection() as conn:
            cursor = conn.cursor()
            
            # Count documents by type
            cursor.execute("""
                SELECT source_type, COUNT(*) as count
                FROM weather_documents
                GROUP BY source_type
            """)
            type_counts = dict(cursor.fetchall())
            
            # Count embeddings
            cursor.execute("SELECT COUNT(*) FROM weather_embeddings")
            embedding_count = cursor.fetchone()[0]
            
            # Get latest sync time
            cursor.execute("""
                SELECT MAX(synced_at) as latest_sync
                FROM weather_documents
            """)
            latest_sync = cursor.fetchone()[0]
            
            return {
                "total_documents": sum(type_counts.values()),
                "alerts": type_counts.get('alert', 0),
                "forecasts": type_counts.get('forecast', 0),
                "total_embeddings": embedding_count,
                "latest_sync": latest_sync.isoformat() if latest_sync else None
            }
    except Exception as e:
        logger.error(f"Failed to get database stats: {e}")
        return {}
#-------------------------------------------------------------------------------------------------------------------------------------------


@app.route("/")
def index():
    """Simple UI to interact with the Weather Intelligence RAG service."""
    return render_template("index.html")


@app.route("/api/health", methods=["GET"])
def health_check():
    """Health check endpoint."""
    return jsonify({
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
    })



@app.route("/weather/stats", methods=["GET"])
def get_stats():
    """Get statistics about stored weather data.
    
    Response JSON:
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
    """
    try:
        stats = get_database_stats()
        return jsonify({
            "status": "success",
            "statistics": stats
        }), 200
    except Exception as e:
        logger.exception("Failed to get statistics")
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500


@app.route("/weather/documents", methods=["GET"])
def list_weather_documents():
    """List synced weather documents from the database.
    
    Query parameters:
        - limit: Maximum number of documents to return (default 100, max 500)
        - source_type: Filter by 'alert' or 'forecast'
    
    Response JSON:
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
    """
    try:
        limit = min(int(request.args.get('limit', 100)), 500)
        source_type = request.args.get('source_type', '').lower()
        
        # Build query with optional filter
        if source_type in ['alert', 'forecast']:
            query = """
                SELECT id, location, source_type, headline, issued_at, synced_at
                FROM weather_documents
                WHERE source_type = %s
                ORDER BY synced_at DESC
                LIMIT %s
            """
            params = (source_type, limit)
        else:
            query = """
                SELECT id, location, source_type, headline, issued_at, synced_at
                FROM weather_documents
                ORDER BY synced_at DESC
                LIMIT %s
            """
            params = (limit,)
        
        with lakebase.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            rows = cursor.fetchall()
        
        documents = []
        for row in rows:
            documents.append({
                "id": row['id'],
                "location": row['location'],
                "source_type": row['source_type'],
                "headline": row['headline'],
                "issued_at": row['issued_at'].isoformat() if row['issued_at'] else None,
                "synced_at": row['synced_at'].isoformat() if row['synced_at'] else None
            })
        
        return jsonify({
            "status": "success",
            "count": len(documents),
            "documents": documents
        }), 200
        
    except Exception as e:
        logger.exception("Failed to list weather documents")
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500


@app.route("/weather/documents/<document_id>", methods=["DELETE"])
def delete_weather_document(document_id: str):
    """Delete a specific weather document by ID.
    
    Response JSON:
        {
            "status": "success",
            "deleted": true,
            "document_id": "abc123..."
        }
    """
    try:
        with lakebase.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM weather_documents WHERE id = %s",
                (document_id,)
            )
            deleted_count = cursor.rowcount
            conn.commit()
        
        if deleted_count == 0:
            return jsonify({
                "status": "error",
                "message": f"Document with ID {document_id} not found"
            }), 404
        
        return jsonify({
            "status": "success",
            "deleted": True,
            "document_id": document_id
        }), 200
        
    except Exception as e:
        logger.exception(f"Failed to delete document {document_id}")
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500


@app.route("/weather/documents/cleanup", methods=["POST"])
def cleanup_old_documents():
    """Delete weather documents older than a specified number of days.
    
    Request JSON:
        {
            "days_old": 30  // Delete documents synced more than 30 days ago
        }
    
    Response JSON:
        {
            "status": "success",
            "deleted_count": 45,
            "message": "Deleted 45 documents older than 30 days"
        }
    """
    try:
        data = request.get_json()
        days_old = int(data.get('days_old', 30))
        
        if days_old < 1:
            return jsonify({
                "status": "error",
                "message": "days_old must be at least 1"
            }), 400
        
        with lakebase.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                DELETE FROM weather_documents
                WHERE synced_at < NOW() - INTERVAL '%s days'
                """,
                (days_old,)
            )
            deleted_count = cursor.rowcount
            conn.commit()
        
        return jsonify({
            "status": "success",
            "deleted_count": deleted_count,
            "message": f"Deleted {deleted_count} documents older than {days_old} days"
        }), 200
        
    except Exception as e:
        logger.exception("Failed to cleanup old documents")
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500


@app.route("/weather/sync", methods=["POST"])
def sync_weather():
    """Sync weather data from NWS API.
    
    Request JSON:
        {
            "locations": ["41.88,-87.63", "IL"],
            "limit": 10
        }
    
    Response JSON:
        {
            "status": "success",
            "statistics": {
                "alerts": 5,
                "forecasts": 14,
                "upserted": 19
            }
        }
    """
    try:
        # Ensure tables exist
        ensure_alerts_table()
        ensure_embeddings_table()
        
        data = request.get_json()
        
        if not data or 'locations' not in data:
            return jsonify({
                "status": "error",
                "message": "Missing 'locations' in request body"
            }), 400
        
        locations = data['locations']
        limit = data.get('limit', 10)
        
        if not isinstance(locations, list) or len(locations) == 0:
            return jsonify({
                "status": "error",
                "message": "'locations' must be a non-empty list"
            }), 400
        
        if not isinstance(limit, int) or limit < 1 or limit > 50:
            return jsonify({
                "status": "error",
                "message": "'limit' must be an integer between 1 and 50"
            }), 400
        
        # Initialize NWS client and sync data
        client = NWSClient()
        stats = client.sync_weather_data(locations, limit)
        
        return jsonify({
            "status": "success",
            "statistics": stats,
            "message": f"Synced {stats.get('upserted', 0)} documents"
        }), 200
        
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500


@app.route("/weather/search", methods=["POST"])
def search_weather():
    """Semantic search over weather documents using vector similarity.
    
    Request JSON:
        {
            "query": "flash flood risk this weekend",
            "top_k": 5
        }
    
    Response JSON:
        {
            "status": "success",
            "query": "flash flood risk this weekend",
            "results": [
                {
                    "location": "Chicago, IL",
                    "source_type": "alert",
                    "headline": "Flash Flood Warning",
                    "chunk_text": "...",
                    "similarity_score": 0.87,
                    "issued_at": "2024-01-15T10:30:00Z"
                }
            ]
        }
    """
    try:
        data = request.get_json()
        
        if not data or 'query' not in data:
            return jsonify({
                "status": "error",
                "message": "Missing 'query' in request body"
            }), 400
        
        query = data['query']
        top_k = data.get('top_k', 5)
        
        if not isinstance(query, str) or len(query.strip()) == 0:
            return jsonify({
                "status": "error",
                "message": "'query' must be a non-empty string"
            }), 400
        
        # Clamp top_k between 1 and 20
        top_k = max(1, min(20, int(top_k)))
        

        # Load model if needed and generate query embedding

        # Lazy-load the embedding model on first use
        global _embedding_model
        if '_embedding_model' not in globals():
            from sentence_transformers import SentenceTransformer
            logger.info(f"Loading embedding model {EMBEDDING_MODEL_NAME}...")
            _embedding_model = SentenceTransformer(EMBEDDING_MODEL_NAME)        

        query_embedding = _embedding_model.encode(
            [query],
            convert_to_numpy=True
        )[0].tolist()
        
        # Convert embedding to pgvector format: '[val1,val2,...]'
        vector_str = '[' + ','.join(str(float(x)) for x in query_embedding) + ']'
        
        # Execute pgvector cosine similarity search
        search_query = """
            SELECT 
                wd.location,
                wd.source_type,
                wd.headline,
                we.chunk_text,
                wd.issued_at,
                1 - (we.embedding <=> %s::vector) AS similarity_score
            FROM weather_embeddings we
            JOIN weather_documents wd ON we.document_id = wd.id
            ORDER BY we.embedding <=> %s::vector
            LIMIT %s
        """
        
        with lakebase.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(search_query, (vector_str, vector_str, top_k))
            rows = cursor.fetchall()
        
        # Format results (rows are dicts from RealDictCursor)
        results = []
        for row in rows:
            results.append({
                "location": row['location'],
                "source_type": row['source_type'],
                "headline": row['headline'],
                "chunk_text": row['chunk_text'],
                "issued_at": row['issued_at'].isoformat() if row['issued_at'] else None,
                "similarity_score": float(row['similarity_score'])
            })
        
        return jsonify({
            "status": "success",
            "query": query,
            "top_k": top_k,
            "results": results,
            "count": len(results)
        }), 200
        
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500





if __name__ == "__main__":
    try:
        # Verify database connectivity on startup
        print("\nVerifying database connectivity...")
        if test_connection():
            print("✓ Database connection successful")

        else:
            print("✗ WARNING: Database connection failed")
            print("   Check Databricks secrets and Lakebase configuration")
        
        # Start Flask server
        port = int(os.getenv("PORT", 8080))
        print(f"\nStarting Weather RAG Service on port {port}...\n")
        app.run(host="0.0.0.0", port=port, debug=False)
    
    except Exception as e:
        print(f"\n✗ FATAL ERROR during startup: {e}")
        import traceback
        traceback.print_exc()
        raise
