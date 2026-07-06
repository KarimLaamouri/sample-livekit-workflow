import { useEffect, useState } from 'react';
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
  status: 'active' | 'ended';
  ended_at: string | null;
};

type JoinState = {
  token: string;
  roomName: string;
  participantName: string;
  role: Role;
  expiresInSeconds: number;
};

type ApiError = Error & {
  status: number;
  code?: string;
};

type ErrorNotice = {
  title: string;
  message: string;
  suggestion: string;
  status?: number;
};

type AuditEvent = {
  timestamp: string;
  event_type: string;
  consultation_id?: string;
  ended_by?: string;
  room_name?: string;
  source?: string;
};

const parseApiError = async (response: Response): Promise<ApiError> => {
  let detail: unknown = null;

  try {
    const payload = await response.json() as { detail?: unknown };
    detail = payload.detail ?? null;
  } catch {
    detail = await response.text();
  }

  let message = 'Unknown error';
  let code: string | undefined;

  if (typeof detail === 'string') {
    message = detail;
  } else if (detail && typeof detail === 'object') {
    if ('message' in detail) {
      message = String((detail as { message: unknown }).message ?? message);
    }

    if ('code' in detail && typeof (detail as { code: unknown }).code === 'string') {
      code = (detail as { code: string }).code;
    }
  } else if (typeof detail === 'number') {
    message = String(detail);
  } else if (typeof detail === 'boolean') {
    message = String(detail);
  } else if (typeof detail === 'object' && detail !== null) {
    message = JSON.stringify(detail);
  } else if (typeof detail === 'string' && detail.length > 0) {
    message = detail;
  }

  const error = new Error(message || `Request failed (${response.status})`) as ApiError;
  error.status = response.status;
  if (code) {
    error.code = code;
  }

  return error;
};

const createErrorNotice = (error: unknown, actionLabel: string): ErrorNotice => {
  if (error instanceof Error && 'status' in error) {
    const apiError = error as ApiError;

    if (apiError.status === 404) {
      return {
        title: 'Consultation not found',
        message: 'The consultation ID does not match any active session.',
        suggestion: 'Check the ID and try again, or create a new consultation.',
        status: apiError.status,
      };
    }

    if (apiError.status === 409 || apiError.status === 410 || apiError.code === 'CONSULTATION_ENDED') {
      return {
        title: 'Consultation no longer available',
        message: 'That consultation has already ended or expired, so new tokens cannot be issued.',
        suggestion: 'Ask the doctor to start a new consultation and use the fresh ID.',
        status: apiError.status,
      };
    }

    if (apiError.status === 403) {
      return {
        title: 'Access denied',
        message: apiError.message,
        suggestion: 'Confirm the participant name and role match the consultation settings.',
        status: apiError.status,
      };
    }

    return {
      title: `${actionLabel} failed`,
      message: apiError.message,
      suggestion: 'Try again in a moment or refresh the page if the problem persists.',
      status: apiError.status,
    };
  }

  return {
    title: `${actionLabel} failed`,
    message: error instanceof Error ? error.message : 'Unexpected error',
    suggestion: 'Try again in a moment or refresh the page if the problem persists.',
  };
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
  const [errorNotice, setErrorNotice] = useState<ErrorNotice | null>(null);
  const [sessionNotice, setSessionNotice] = useState<ErrorNotice | null>(null);
  const [busy, setBusy] = useState(false);

  const markConsultationEnded = (endedAt: string, notice: ErrorNotice) => {
    setConsultation((current) => {
      if (!current || current.status === 'ended') {
        return current;
      }

      return {
        ...current,
        status: 'ended',
        ended_at: endedAt,
      };
    });

    setSessionNotice(notice);
    setStatus(notice.message);

    if (joinState !== null) {
      setJoinState(null);
    }
  };

  const leaveCall = () => {
    setJoinState(null);
    setStatus('Call ended. Request a fresh token to rejoin if the consultation is still active.');
  };

  const requestJson = async <T,>(url: string, init?: RequestInit): Promise<T> => {
    const response = await fetch(url, {
      headers: { 'Content-Type': 'application/json' },
      ...init,
    });

    if (!response.ok) {
      throw await parseApiError(response);
    }

    return response.json() as Promise<T>;
  };

  useEffect(() => {
    if (!consultation || consultation.status === 'ended') {
      return;
    }

    const expiresAtMs = Date.parse(consultation.expires_at);
    if (Number.isNaN(expiresAtMs)) {
      return;
    }

    const expireConsultation = () => {
      markConsultationEnded(consultation.expires_at, {
        title: 'Consultation expired',
        message: 'This consultation reached its scheduled end time.',
        suggestion: 'Create a new consultation to continue.',
      });
    };

    const timeUntilExpiry = expiresAtMs - Date.now();
    if (timeUntilExpiry <= 0) {
      expireConsultation();
      return;
    }

    const timeoutId = window.setTimeout(expireConsultation, timeUntilExpiry);
    return () => window.clearTimeout(timeoutId);
  }, [consultation]);

  useEffect(() => {
    if (!consultation || consultation.status === 'ended') {
      return;
    }

    let cancelled = false;
    const consultationIdForWatch = consultation.consultation_id;

    const syncConsultationState = async () => {
      try {
        const response = await fetch(`${API_URL}/api/audit-events`);

        if (!response.ok) {
          return;
        }

        const events = await response.json() as AuditEvent[];

        if (cancelled) {
          return;
        }

        const latestMatchingEvent = [...events].reverse().find((event) => {
          if (event.consultation_id !== consultationIdForWatch) {
            return false;
          }

          return event.event_type === 'consultation.ended' || event.event_type === 'consultation.expired';
        });

        if (!latestMatchingEvent) {
          return;
        }

        if (latestMatchingEvent.event_type === 'consultation.ended') {
          markConsultationEnded(latestMatchingEvent.timestamp, {
            title: 'Consultation ended elsewhere',
            message: `This consultation was ended by ${latestMatchingEvent.ended_by ?? 'another window'}.`,
            suggestion: 'Request a new consultation ID if you need to rejoin.',
          });
          return;
        }

        markConsultationEnded(consultation.expires_at, {
          title: 'Consultation expired',
          message: 'This consultation expired in the backend.',
          suggestion: 'Create a new consultation to continue.',
        });
      } catch {
        if (!cancelled) {
          return;
        }
      }
    };

    void syncConsultationState();
    const intervalId = window.setInterval(syncConsultationState, 5000);

    return () => {
      cancelled = true;
      window.clearInterval(intervalId);
    };
  }, [consultation?.consultation_id, consultation?.expires_at, consultation?.status, joinState]);

  const endConsultation = async () => {
    if (!joinState || !consultationId.trim()) {
      return;
    }

    setBusy(true);
    setErrorNotice(null);
    setSessionNotice(null);

    try {
      const ended = await requestJson<{
        consultation_id: string;
        room_name: string;
        status: 'ended';
        ended_at: string;
        ended_by: string;
      }>(`${API_URL}/api/consultations/${encodeURIComponent(consultationId.trim())}/end`, {
        method: 'POST',
        body: JSON.stringify({
          participant_name: joinState.participantName,
          role: joinState.role,
        }),
      });

      setConsultation((current) => {
        if (!current) {
          return current;
        }

        return {
          ...current,
          status: ended.status,
          ended_at: ended.ended_at,
        };
      });
      setJoinState(null);
      setStatus(`Consultation ended by ${ended.ended_by}. Rejoin is now blocked.`);
    } catch (e) {
      setErrorNotice(createErrorNotice(e, 'Ending the consultation'));
    } finally {
      setBusy(false);
    }
  };

  const createConsultation = async () => {
    setBusy(true);
    setErrorNotice(null);
    setSessionNotice(null);

    try {
      const created = await requestJson<Consultation>(`${API_URL}/api/consultations`, {
        method: 'POST',
        body: JSON.stringify({ doctor_name: doctorName, patient_name: patientName }),
      });

      setConsultation(created);
      setConsultationId(created.consultation_id);
      setStatus('Consultation created. Copy the consultation ID into another browser to join as patient.');
    } catch (e) {
      setErrorNotice(createErrorNotice(e, 'Creating the consultation'));
    } finally {
      setBusy(false);
    }
  };

  const joinConsultation = async () => {
    const id = consultationId.trim();

    if (!id) {
      setErrorNotice({
        title: 'Consultation ID required',
        message: 'Paste a consultation ID before trying to issue a token.',
        suggestion: 'Use the ID from the Create consultation panel.',
      });
      return;
    }

    setBusy(true);
    setErrorNotice(null);
    setSessionNotice(null);

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
      setErrorNotice(createErrorNotice(e, 'Joining the consultation'));
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
          <div>
            {joinState.role === 'doctor' && (
              <button type="button" onClick={endConsultation} disabled={busy}>End consultation</button>
            )}
            <button type="button" onClick={leaveCall}>Leave test</button>
          </div>
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
        {errorNotice && (
          <div className="notice-card" role="alert" aria-live="polite">
            <div className="notice-copy">
              <p className="notice-title">{errorNotice.title}</p>
              <p className="notice-message">{errorNotice.message}</p>
              <p className="notice-suggestion">{errorNotice.suggestion}</p>
            </div>
            <div className="notice-actions">
              {typeof errorNotice.status === 'number' && (
                <span className="notice-badge">HTTP {errorNotice.status}</span>
              )}
              <button type="button" className="ghost-button" onClick={() => setErrorNotice(null)}>
                Dismiss
              </button>
            </div>
          </div>
        )}
        {!errorNotice && sessionNotice && (
          <div className="notice-card notice-card--info" aria-live="polite">
            <div className="notice-copy">
              <p className="notice-title">{sessionNotice.title}</p>
              <p className="notice-message">{sessionNotice.message}</p>
              <p className="notice-suggestion">{sessionNotice.suggestion}</p>
            </div>
            <div className="notice-actions">
              <button type="button" className="ghost-button" onClick={() => setSessionNotice(null)}>
                Dismiss
              </button>
            </div>
          </div>
        )}
        {consultation ? (
          <dl>
            <div><dt>Consultation ID</dt><dd>{consultation.consultation_id}</dd></div>
            <div><dt>LiveKit room</dt><dd>{consultation.room_name}</dd></div>
            <div><dt>Session expires</dt><dd>{new Date(consultation.expires_at).toLocaleString()}</dd></div>
            <div><dt>Status</dt><dd>{consultation.status}</dd></div>
            <div><dt>Ended at</dt><dd>{consultation.ended_at ? new Date(consultation.ended_at).toLocaleString() : 'Not ended'}</dd></div>
            <div><dt>Token TTL</dt><dd>{Math.round(consultation.token_ttl_seconds / 60)} minutes</dd></div>
          </dl>
        ) : (
          <p>{status}</p>
        )}
      </section>
    </main>
  );
}

export default App;
