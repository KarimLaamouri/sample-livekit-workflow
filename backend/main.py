import os
from fastapi import FastAPI, HTTPException
from livekit import api # Updated import

app = FastAPI()

@app.get("/api/get-token")
async def get_token(room_name: str, participant_name: str):
    # The SDK automatically looks for LIVEKIT_API_KEY and LIVEKIT_API_SECRET in your environment variables
    api_key = os.getenv("LIVEKIT_API_KEY")
    api_secret = os.getenv("LIVEKIT_API_SECRET")

    if not api_key or not api_secret:
        raise HTTPException(status_code=500, detail="LiveKit credentials missing")

    # Define what the user is allowed to do (VideoGrants)
    grant = api.VideoGrants(
        room_join=True, 
        room=room_name
    )

    # Generate and sign the token
    access_token = (
        api.AccessToken()
        .with_identity(participant_name)
        .with_name(participant_name)
        .with_grants(grant)
    )
    
    return {"token": access_token.to_jwt()}