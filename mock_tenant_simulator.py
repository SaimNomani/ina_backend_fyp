import asyncio
import httpx
import json

# --- CONFIGURATION ---
# 1. The URL of your local backend (or use your Railway URL for live testing)
API_URL = "http://127.0.0.1:8000" 

# 2. A Valid API Key from your Database (Week 2, Day 1)
# You MUST replace this with a real key from your 'tenants' table.
TEST_API_KEY = "ina_key_f824a6b9b8b997bb66a070a5c8d021e9" 

async def start_session():
    """
    Simulates a Tenant pushing data to the Ina Backend to start a negotiation.
    """
    endpoint = f"{API_URL}/api/v1/session/init"
    
    # The data the tenant "Push" to us
    payload = {
        "api_key": TEST_API_KEY,
        "context_id": "test_user_001",  # A fake user ID
        "mam": 45000.0,                 # Minimum Acceptable Margin (The "Floor")
        "asking_price": 50000.0         # The starting price
    }

    print(f"🔵 Attempting to start session at: {endpoint}")
    print(f"📦 Payload: {json.dumps(payload, indent=2)}")

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(endpoint, json=payload)
            
            # Check for success
            if response.status_code == 200:
                data = response.json()
                print("\n✅ SUCCESS! Session Created.")
                print(f"🔑 Session ID: {data['session_id']}")
                print("-" * 40)
                print("Give this Session ID to the AI Orchestrator to begin chatting.")
                print("-" * 40)
            else:
                print(f"\n❌ FAILED. Status: {response.status_code}")
                print(f"Error: {response.text}")

        except Exception as e:
            print(f"\n❌ Connection Error: {e}")
            print("Is the backend server running?")

if __name__ == "__main__":
    asyncio.run(start_session())