from fastapi import FastAPI

app = FastAPI()

@app.get("/{context_id}")
def get_policy(context_id: str):
    return {"mam": 150.0, "asking_price": 200.0}
