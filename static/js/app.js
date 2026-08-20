// Weather Intelligence RAG - Frontend JavaScript

// API Base URL (adjust if needed)
const API_BASE = '';

// DOM Elements
const syncForm = document.getElementById('syncForm');
const syncBtn = document.getElementById('syncBtn');
const syncResult = document.getElementById('syncResult');

const searchForm = document.getElementById('searchForm');
const searchBtn = document.getElementById('searchBtn');
const searchResults = document.getElementById('searchResults');

const healthStatus = document.getElementById('healthStatus');

// Initialize
document.addEventListener('DOMContentLoaded', () => {
    checkHealth();
    
    // Set up form handlers
    syncForm.addEventListener('submit', handleSync);
    searchForm.addEventListener('submit', handleSearch);
    
    // Check health every 30 seconds
    setInterval(checkHealth, 30000);
});

// Health Check
async function checkHealth() {
    try {
        const response = await fetch(`${API_BASE}/health/database`);
        const data = await response.json();
        
        const statusDot = healthStatus.querySelector('.status-dot');
        const statusText = healthStatus.querySelector('.status-text');
        
        if (data.database_connected && data.pgvector_enabled) {
            statusDot.className = 'status-dot healthy';
            statusText.textContent = 'System Healthy';
        } else {
            statusDot.className = 'status-dot unhealthy';
            statusText.textContent = 'System Offline';
        }
    } catch (error) {
        const statusDot = healthStatus.querySelector('.status-dot');
        const statusText = healthStatus.querySelector('.status-text');
        statusDot.className = 'status-dot unhealthy';
        statusText.textContent = 'Connection Error';
    }
}

// Handle Sync Form
async function handleSync(e) {
    e.preventDefault();
    
    const locationsInput = document.getElementById('locations').value;
    const limit = parseInt(document.getElementById('limit').value);
    
    // Parse locations (split by comma)
    const locations = locationsInput.split(',').map(loc => loc.trim()).filter(loc => loc);
    
    if (locations.length === 0) {
        showResult(syncResult, 'Please enter at least one location', 'error');
        return;
    }
    
    // Disable button and show loading
    const originalText = syncBtn.innerHTML;
    syncBtn.disabled = true;
    syncBtn.innerHTML = '<span class="loading"></span> Syncing...';
    
    try {
        const response = await fetch(`${API_BASE}/weather/sync`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ locations, limit })
        });
        
        const data = await response.json();
        
        if (response.ok) {
            const stats = data.statistics;
            const message = `
                ✓ Sync Complete<br>
                <strong>${stats.upserted || 0}</strong> documents synced<br>
                Alerts: ${stats.alerts || 0} | Forecasts: ${stats.forecasts || 0}
                ${stats.errors > 0 ? `<br>⚠ Errors: ${stats.errors}` : ''}
            `;
            showResult(syncResult, message, 'success');
        } else {
            showResult(syncResult, `Error: ${data.message}`, 'error');
        }
    } catch (error) {
        showResult(syncResult, `Network error: ${error.message}`, 'error');
    } finally {
        syncBtn.disabled = false;
        syncBtn.innerHTML = originalText;
    }
}

// Handle Search Form
async function handleSearch(e) {
    e.preventDefault();
    
    const query = document.getElementById('query').value.trim();
    const topK = parseInt(document.getElementById('topK').value);
    
    if (!query) {
        showResult(searchResults, 'Please enter a search query', 'error');
        return;
    }
    
    // Disable button and show loading
    const originalText = searchBtn.innerHTML;
    searchBtn.disabled = true;
    searchBtn.innerHTML = '<span class="loading"></span> Searching...';
    
    // Clear previous results
    searchResults.innerHTML = '<div class="result-message info show">Searching...</div>';
    
    try {
        const response = await fetch(`${API_BASE}/weather/search`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ query, top_k: topK })
        });
        
        const data = await response.json();
        
        if (response.ok) {
            displayResults(data);
        } else {
            searchResults.innerHTML = `
                <div class="result-message error show">
                    Error: ${data.message}
                </div>
            `;
        }
    } catch (error) {
        searchResults.innerHTML = `
            <div class="result-message error show">
                Network error: ${error.message}
            </div>
        `;
    } finally {
        searchBtn.disabled = false;
        searchBtn.innerHTML = originalText;
    }
}

// Display Search Results
function displayResults(data) {
    if (!data.results || data.results.length === 0) {
        searchResults.innerHTML = `
            <div class="no-results">
                <svg viewBox="0 0 24 24" fill="currentColor">
                    <path d="M9.172 16.172a4 4 0 015.656 0M9 10h.01M15 10h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/>
                </svg>
                <p>No results found for "${escapeHtml(data.query)}"</p>
                <small>Try a different query or sync more data first</small>
            </div>
        `;
        return;
    }
    
    let html = `
        <div class="result-message info show">
            Found <strong>${data.count}</strong> result(s) for "<strong>${escapeHtml(data.query)}</strong>"
        </div>
    `;
    
    data.results.forEach((result, index) => {
        const scorePercent = Math.round(result.similarity_score * 100);
        const badgeClass = result.source_type === 'alert' ? 'badge-alert' : 'badge-forecast';
        
        html += `
            <div class="result-item">
                <div class="result-header">
                    <div>
                        <h3 class="result-headline">${escapeHtml(result.headline) || 'No headline'}</h3>
                        <div class="result-meta">
                            <div class="meta-item">
                                <svg width="12" height="12" viewBox="0 0 12 12" fill="currentColor">
                                    <path d="M6 1a1 1 0 011 1v4a1 1 0 11-2 0V2a1 1 0 011-1zm0 8a1 1 0 100 2 1 1 0 000-2z"/>
                                </svg>
                                ${escapeHtml(result.location)}
                            </div>
                            <span class="badge ${badgeClass}">${result.source_type}</span>
                            ${result.issued_at ? `
                                <div class="meta-item">
                                    <svg width="12" height="12" viewBox="0 0 12 12" fill="currentColor">
                                        <path d="M6 1a5 5 0 100 10A5 5 0 006 1zM5 3.5a.5.5 0 011 0V6h1.5a.5.5 0 010 1H5.5a.5.5 0 01-.5-.5v-3z"/>
                                    </svg>
                                    ${formatDate(result.issued_at)}
                                </div>
                            ` : ''}
                        </div>
                    </div>
                    <div class="similarity-score">
                        <span>${scorePercent}%</span>
                        <div class="score-bar">
                            <div class="score-fill" style="width: ${scorePercent}%"></div>
                        </div>
                    </div>
                </div>
                <div class="result-text">
                    ${escapeHtml(result.chunk_text)}
                </div>
            </div>
        `;
    });
    
    searchResults.innerHTML = html;
}

// Utility: Show Result Message
function showResult(element, message, type) {
    element.innerHTML = message;
    element.className = `result-message ${type} show`;
    
    // Auto-hide after 5 seconds for success messages
    if (type === 'success') {
        setTimeout(() => {
            element.classList.remove('show');
        }, 5000);
    }
}

// Utility: Escape HTML
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// Utility: Format Date
function formatDate(isoString) {
    const date = new Date(isoString);
    const now = new Date();
    const diffMs = now - date;
    const diffMins = Math.floor(diffMs / 60000);
    const diffHours = Math.floor(diffMs / 3600000);
    const diffDays = Math.floor(diffMs / 86400000);
    
    if (diffMins < 60) {
        return `${diffMins}m ago`;
    } else if (diffHours < 24) {
        return `${diffHours}h ago`;
    } else if (diffDays < 7) {
        return `${diffDays}d ago`;
    } else {
        return date.toLocaleDateString();
    }
}
