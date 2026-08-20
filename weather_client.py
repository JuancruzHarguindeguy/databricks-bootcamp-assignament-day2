"""NWS (National Weather Service) API client for weather data ingestion."""


import os



import lakebase

import hashlib
import json
import time
from datetime import datetime
from typing import List, Dict, Optional, Tuple
import requests

# NWS API configuration
_BASE_URL = os.environ.get("WEATHER_API_BASE_URL", "https://api.weather.gov")
_USER_AGENT = "(WeatherRAGService, contact@example.com)"  # NWS requires User-Agent

_DEFAULT_DELAY = 2

class NWSClient:
    """Client for interacting with the National Weather Service API."""  
    
    def __init__(self, retry_delay: int = _DEFAULT_DELAY):
        self.retry_delay = retry_delay
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": _USER_AGENT})
    
    def _make_request(self, url: str, max_retries: int = 3) -> Optional[Dict]:
        """Make HTTP request with retry logic.
        
        Args:
            url: Full URL to request
            max_retries: Maximum number of retry attempts
            
        Returns:
            JSON response as dict, or None on failure
        """
        for attempt in range(max_retries):
            try:
                response = self.session.get(url, timeout=10)
                
                if response.status_code == 200:
                    return response.json()
                elif response.status_code == 429:  # Rate limited
                    wait_time = self.retry_delay * (attempt + 1)
                    print(f"Rate limited. Waiting {wait_time}s before retry...")
                    time.sleep(wait_time)
                elif response.status_code == 503:  # Service unavailable
                    print(f"NWS service unavailable. Retrying...")
                    time.sleep(self.retry_delay)
                else:
                    print(f"Request failed with status {response.status_code}: {url}")
                    return None
                    
            except requests.RequestException as e:
                print(f"Request error (attempt {attempt + 1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    time.sleep(self.retry_delay)
        
        return None
    
    def geocode_location(self, location: str) -> Optional[Tuple[float, float, str, int, int]]:
        """Convert location string to coordinates and NWS grid.
        
        Args:
            location: Either "City, State" or "lat,lon"
            
        Returns:
            Tuple of (lat, lon, office, gridX, gridY) or None on failure
        """
        # Check if already coordinates
        if ',' in location and location.replace(',', '').replace('.', '').replace('-', '').replace(' ', '').isdigit():
            parts = location.split(',')
            lat, lon = float(parts[0].strip()), float(parts[1].strip())
        else:
            # For simplicity, this example requires lat,lon input
            # Production would use a geocoding service (e.g., Nominatim)
            print(f"Location geocoding not implemented. Please use lat,lon format.")
            return None
        
        # Query NWS /points endpoint to get grid information
        points_url = f"{_BASE_URL}/points/{lat},{lon}"
        data = self._make_request(points_url)
        
        if not data or 'properties' not in data:
            print(f"Failed to get grid information for {lat},{lon}")
            return None
        
        props = data['properties']
        office = props.get('gridId')
        grid_x = props.get('gridX')
        grid_y = props.get('gridY')
        
        if not all([office, grid_x, grid_y]):
            print(f"Incomplete grid data for {lat},{lon}")
            return None
        
        return lat, lon, office, grid_x, grid_y
    
    def fetch_alerts(self, state: str, limit: int = 10) -> List[Dict]:
        """Fetch active weather alerts for a state.
        
        Args:
            state: Two-letter state code (e.g., 'IL', 'TX')
            limit: Maximum number of alerts to return
            
        Returns:
            List of alert dictionaries
        """
        alerts_url = f"{_BASE_URL}/alerts/active?area={state.upper()}"
        data = self._make_request(alerts_url)
        
        if not data or 'features' not in data:
            return []
        
        alerts = []
        for feature in data['features'][:limit]:
            props = feature.get('properties', {})
            
            # Extract relevant fields
            alert = {
                'id': props.get('id', ''),
                'location': props.get('areaDesc', f"{state.upper()}"),
                'headline': props.get('headline', ''),
                'description': props.get('description', ''),
                'instruction': props.get('instruction', ''),
                'severity': props.get('severity', ''),
                'issued_at': props.get('sent', datetime.utcnow().isoformat()),
                'payload': props
            }
            alerts.append(alert)
        
        return alerts
    
    def fetch_forecast(self, office: str, grid_x: int, grid_y: int) -> List[Dict]:
        """Fetch detailed forecast for a grid location.
        
        Args:
            office: NWS office code (e.g., 'LOT')
            grid_x: Grid X coordinate
            grid_y: Grid Y coordinate
            
        Returns:
            List of forecast period dictionaries
        """
        forecast_url = f"{_BASE_URL}/gridpoints/{office}/{grid_x},{grid_y}/forecast"
        data = self._make_request(forecast_url)
        
        if not data or 'properties' not in data or 'periods' not in data['properties']:
            return []
        
        forecasts = []
        for period in data['properties']['periods']:
            forecast = {
                'location': f"{office}_{grid_x}_{grid_y}",
                'headline': period.get('name', ''),
                'detailed_forecast': period.get('detailedForecast', ''),
                'temperature': period.get('temperature'),
                'wind_speed': period.get('windSpeed', ''),
                'issued_at': data['properties'].get('updated', datetime.utcnow().isoformat()),
                'payload': period
            }
            forecasts.append(forecast)
        
        return forecasts
    
    @staticmethod
    def generate_document_id(location: str, issued_at: str, source_type: str) -> str:
        """Generate deterministic document ID for deduplication.
        
        Args:
            location: Location string
            issued_at: ISO timestamp
            source_type: 'alert' or 'forecast'
            
        Returns:
            SHA256 hash as document ID
        """
        key = f"{location}_{issued_at}_{source_type}"
        return hashlib.sha256(key.encode()).hexdigest()
    
    def upsert_documents(self, documents: List[Dict]) -> int:
        """Insert or update weather documents in database.
        
        Args:
            documents: List of document dictionaries with keys:
                - location, source_type, headline, narrative_text, issued_at, payload
                
        Returns:
            Number of documents upserted
        """
        if not documents:
            return 0
        
        upsert_query = """
            INSERT INTO weather_documents 
                (id, location, source_type, headline, narrative_text, issued_at, payload)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) 
            DO UPDATE SET
                location = EXCLUDED.location,
                headline = EXCLUDED.headline,
                narrative_text = EXCLUDED.narrative_text,
                synced_at = CURRENT_TIMESTAMP,
                payload = EXCLUDED.payload
        """
        
        count = 0
        with lakebase.get_connection() as conn:
            cursor = conn.cursor()
            
            for doc in documents:
                doc_id = self.generate_document_id(
                    doc['location'],
                    doc['issued_at'],
                    doc['source_type']
                )
                
                cursor.execute(upsert_query, (
                    doc_id,
                    doc['location'],
                    doc['source_type'],
                    doc.get('headline', ''),
                    doc['narrative_text'],
                    doc['issued_at'],
                    json.dumps(doc.get('payload', {}))
                ))
                count += 1
            
            # Commit the transaction
            conn.commit()
        
        return count
    
    def sync_weather_data(self, locations: List[str], limit: int = 10) -> Dict[str, int]:
        """Sync weather data for multiple locations.
        
        Args:
            locations: List of location strings (lat,lon or City,State)
            limit: Max items per location
            
        Returns:
            Dictionary with sync statistics
        """
        stats = {'alerts': 0, 'forecasts': 0, 'errors': 0}
        all_documents = []
        
        for location in locations:
            try:
                # Try to parse as state for alerts
                if len(location) == 2 and location.isalpha():
                    alerts = self.fetch_alerts(location, limit)
                    for alert in alerts:
                        # Build narrative_text, only add instruction if non-empty
                        narrative_parts = [alert['description']]
                        if alert['instruction']:
                            narrative_parts.append(alert['instruction'])
                        
                        doc = {
                            'location': alert['location'],
                            'source_type': 'alert',
                            'headline': alert['headline'],
                            'narrative_text': '\n\n'.join(narrative_parts),
                            'issued_at': alert['issued_at'],
                            'payload': alert['payload']
                        }
                        all_documents.append(doc)
                        stats['alerts'] += 1
                
                # Try geocoding for forecast (skip if it's a 2-letter state code)
                if not (len(location) == 2 and location.isalpha()):
                    grid_info = self.geocode_location(location)
                else:
                    grid_info = None
                if grid_info:
                    lat, lon, office, grid_x, grid_y = grid_info
                    forecasts = self.fetch_forecast(office, grid_x, grid_y)
                    
                    for forecast in forecasts[:limit]:
                        doc = {
                            'location': f"{lat},{lon}",
                            'source_type': 'forecast',
                            'headline': forecast['headline'],
                            'narrative_text': forecast['detailed_forecast'],
                            'issued_at': forecast['issued_at'],
                            'payload': forecast['payload']
                        }
                        all_documents.append(doc)
                        stats['forecasts'] += 1
                        
            except Exception as e:
                print(f"Error processing location {location}: {e}")
                stats['errors'] += 1
        
        # Bulk upsert
        if all_documents:
            upserted = self.upsert_documents(all_documents)
            stats['upserted'] = upserted
        
        return stats


if __name__ == "__main__":
    # Test the client
    client = NWSClient()
    
    # Example: Sync Chicago weather
    print("Syncing weather data for Chicago, IL...")
    stats = client.sync_weather_data(["41.88,-87.63", "IL"], limit=5)
    print(f"Sync complete: {stats}")
