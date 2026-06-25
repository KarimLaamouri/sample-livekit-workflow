import { useState, useEffect } from 'react';
import {
  LiveKitRoom,
  VideoConference,
  RoomAudioRenderer,
} from '@livekit/components-react';
import '@livekit/components-styles';

// Point this to your OVH VM URL, e.g., wss://video.tachafy.com
const LIVEKIT_URL = 'wss://your-livekit-server-url.com'; 

function App() {
  const [token, setToken] = useState<string | null>(null);
  
  // Hardcoded for testing. In production, this comes from your auth state!
  const roomName = 'consultation-room-1';
  const participantName = `Dr. User_${Math.floor(Math.random() * 100)}`;

  useEffect(() => {
    // 1. Fetch the token from our FastAPI backend
    const fetchToken = async () => {
      try {
        const response = await fetch(
          `http://localhost:8000/api/get-token?room_name=${roomName}&participant_name=${participantName}`
        );
        const data = await response.json();
        setToken(data.token);
      } catch (e) {
        console.error('Error fetching token:', e);
      }
    };

    fetchToken();
  }, []);

  if (token === null) {
    return <div>Loading Teleconsultation...</div>;
  }

  return (
    <div style={{ height: '100vh' }}>
      {/* 2. Connect to LiveKit using the Token */}
      <LiveKitRoom
        video={true}
        audio={true}
        token={token}
        serverUrl={LIVEKIT_URL}
        data-lk-theme="default"
        style={{ height: '100dvh' }}
      >
        {/* 3. Render the pre-built UI */}
        <VideoConference />
        {/* Automatically plays remote audio */}
        <RoomAudioRenderer />
      </LiveKitRoom>
    </div>
  );
}

export default App;