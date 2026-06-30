import { useState } from 'react';
import {
  LiveKitRoom,
  VideoConference,
  RoomAudioRenderer,
} from '@livekit/components-react';
import '@livekit/components-styles';
import './App.css';

const API_URL = 'http://localhost:8000';
const LIVEKIT_URL = 'ws://localhost:7880';

type Role = 'doctor' | 'patient' | 'observer';

type Consultation = {
  consultation_id: string;
  room_name: string;
  expires_at: string;
  token_ttl_seconds: number;
};

type JoinState = {
  token: string;
  roomName: string;
  participantName: string;
  role: Role;
  expiresInSeconds: number;
};

function App() {
  const [doctorName, setDoctorName] = useState('Dr. Tachafy');
  const [patientName, setPatientName] = useState('Patient Demo');
  const [participantName, setParticipantName] = useState('Dr. Tachafy');
  const [role, setRole] = useState<Role>('doctor');
  const [consultationId, setConsultationId] = useState('');
  const [consultation, setConsultation] = useState<Consultation | null>(null);
  const [joinState, setJoinState] = useState<JoinState | null>(null);
  const [status, setStatus] = useState('Create a consultation, then join it from one or two browser windows.');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const leaveCall = () => {
    setJoinState(null);
    setStatus('Call ended. You can request a new LiveKit token with the same consultation ID while the consultation is still valid.');
  };

  const requestJson = async <T,>(url: string, init?: RequestInit): Promise<T> => {
    const response = await fetch(url, {
      headers: { 'Content-Type': 'application/json' },
      ...init,
    });

    if (!response.ok) {
      const message = await response.text();
      throw new Error(`Request failed (${response.status}): ${message}`);
    }

    return response.json() as Promise<T>;
  };

  const createConsultation = async () => {
    setBusy(true);
    setError(null);

    try {
      const created = await requestJson<Consultation>(`${API_URL}/api/consultations`, {
        method: 'POST',
        body: JSON.stringify({ doctor_name: doctorName, patient_name: patientName }),
      });

      setConsultation(created);
      setConsultationId(created.consultation_id);
      setStatus('Consultation created. Copy the consultation ID into another browser to join as patient.');
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not create consultation');
    } finally {
      setBusy(false);
    }
  };

  const joinConsultation = async () => {
    const id = consultationId.trim();

    if (!id) {
      setError('Consultation ID is required');
      return;
    }

    setBusy(true);
    setError(null);

    try {
      const tokenResponse = await requestJson<{
        token: string;
        room_name: string;
        participant_name: string;
        role: Role;
        expires_in_seconds: number;
      }>(`${API_URL}/api/consultations/${encodeURIComponent(id)}/token`, {
        method: 'POST',
        body: JSON.stringify({ participant_name: participantName, role }),
      });

      setJoinState({
        token: tokenResponse.token,
        roomName: tokenResponse.room_name,
        participantName: tokenResponse.participant_name,
        role: tokenResponse.role,
        expiresInSeconds: tokenResponse.expires_in_seconds,
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not join consultation');
    } finally {
      setBusy(false);
    }
  };

  if (joinState !== null) {
    return (
      <div className="call-shell">
        <div className="call-strip">
          <div>
            <strong>{joinState.participantName}</strong>
            <span>{joinState.role} · {joinState.roomName} · token TTL {Math.round(joinState.expiresInSeconds / 60)} min</span>
          </div>
          <button type="button" onClick={() => setJoinState(null)}>Leave test</button>
        </div>
        <LiveKitRoom
          video={joinState.role !== 'observer'}
          audio={joinState.role !== 'observer'}
          token={joinState.token}
          serverUrl={LIVEKIT_URL}
          onDisconnected={leaveCall}
          data-lk-theme="default"
          style={{ height: 'calc(100dvh - 56px)' }}
        >
          <VideoConference />
          <RoomAudioRenderer />
        </LiveKitRoom>
      </div>
    );
  }

  return (
    <main className="teleconsultation-console">
      <section className="intro-panel">
        <p className="eyebrow">Tachafy LiveKit MVP</p>
        <h1>Teleconsultation access console</h1>
        <p>
          Create an isolated consultation room, issue short-lived role-based tokens, then join from two browser windows to test doctor/patient interaction.
        </p>
      </section>

      <section className="workflow-grid">
        <form className="panel" onSubmit={(event) => { event.preventDefault(); createConsultation(); }}>
          <div className="panel-heading">
            <span>1</span>
            <h2>Create consultation</h2>
          </div>
          <label>
            Doctor name
            <input value={doctorName} onChange={(event) => setDoctorName(event.target.value)} />
          </label>
          <label>
            Patient name
            <input value={patientName} onChange={(event) => setPatientName(event.target.value)} />
          </label>
          <button type="submit" disabled={busy}>{busy ? 'Working...' : 'Create secure room'}</button>
        </form>

        <form className="panel" onSubmit={(event) => { event.preventDefault(); joinConsultation(); }}>
          <div className="panel-heading">
            <span>2</span>
            <h2>Join with role</h2>
          </div>
          <label>
            Consultation ID
            <input value={consultationId} onChange={(event) => setConsultationId(event.target.value)} placeholder="Paste ID here" />
          </label>
          <label>
            Participant display name
            <input value={participantName} onChange={(event) => setParticipantName(event.target.value)} />
          </label>
          <label>
            Role
            <select value={role} onChange={(event) => setRole(event.target.value as Role)}>
              <option value="doctor">Doctor: publish + subscribe</option>
              <option value="patient">Patient: publish + subscribe</option>
              <option value="observer">Observer: subscribe only</option>
            </select>
          </label>
          <button type="submit" disabled={busy}>{busy ? 'Working...' : 'Issue token and join'}</button>
        </form>
      </section>

      <section className="status-panel">
        <h2>Session state</h2>
        {consultation ? (
          <dl>
            <div><dt>Consultation ID</dt><dd>{consultation.consultation_id}</dd></div>
            <div><dt>LiveKit room</dt><dd>{consultation.room_name}</dd></div>
            <div><dt>Session expires</dt><dd>{new Date(consultation.expires_at).toLocaleString()}</dd></div>
            <div><dt>Token TTL</dt><dd>{Math.round(consultation.token_ttl_seconds / 60)} minutes</dd></div>
          </dl>
        ) : (
          <p>{status}</p>
        )}
        {error && <p className="error-text">{error}</p>}
      </section>
    </main>
  );
}

export default App;
