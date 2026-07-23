import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  LiveKitRoom,
  RoomAudioRenderer,
  PreJoin,
  useChat,
  GridLayout,
  ParticipantTile,
  ControlBar,
  useTracks,
} from '@livekit/components-react';
import '@livekit/components-styles';
import { ExternalE2EEKeyProvider, Room, Track } from 'livekit-client';
import { MicOff, UserX } from 'lucide-react';
import './App.css';

const API_URL = import.meta.env.VITE_API_URL ?? 'http://localhost:8000';
const LIVEKIT_URL = import.meta.env.VITE_LIVEKIT_URL ?? 'ws://localhost:7880';

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
  consultationId: string;
  token: string | null;
  roomName: string | null;
  participantName: string;
  role: Role;
  expiresInSeconds: number | null;
  e2eeKey: string | null;
  tokenIssuedAt: string | null;
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

type WaitingRoomEntryData = {
  participant_name: string;
  role: Role;
  status: 'waiting' | 'admitted' | 'denied';
  requested_at: string;
};

type ChatMessageResponse = {
  sender_identity: string;
  sender_name: string;
  sender_role: string;
  body: string;
  sent_at: string;
};

type ParticipantInfo = {
  participant_id: string;
  identity: string;
  role: string | null;
  name: string | null;
  state: string | null;
  joined_at: string | null;
  is_publisher: boolean | null;
  tracks: Array<{ type: string; source: string; muted: boolean }> | null;
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
    const errorDetail = detail as { message?: unknown; code?: unknown };

    if ('message' in errorDetail) {
      message = String(errorDetail.message ?? message);
    } else if (!('code' in errorDetail)) {
      message = JSON.stringify(errorDetail);
    }

    if (typeof errorDetail.code === 'string') {
      code = errorDetail.code;
    }
  } else if (typeof detail === 'number' || typeof detail === 'boolean') {
    message = String(detail);
  } else if (detail !== null) {
    message = String(detail);
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
      if (apiError.code === 'ROOM_LOCKED') {
        return {
          title: 'Room locked',
          message: apiError.message || 'The consultation room is locked. Only the doctor can join at this time.',
          suggestion: 'Contact the doctor to unlock the room if you need to join.',
          status: apiError.status,
        };
      }

      if (apiError.code === 'NOT_ADMITTED') {
        return {
          title: 'Not admitted',
          message: apiError.message || 'You have not been admitted to this consultation. Please request access via the waiting room.',
          suggestion: 'Request access through the waiting room and wait for the doctor to admit you.',
          status: apiError.status,
        };
      }

      if (apiError.code === 'DOCTOR_BYPASS') {
        return {
          title: 'Doctor bypass',
          message: apiError.message || 'Doctors do not use the waiting room.',
          suggestion: 'Doctors can join directly without using the waiting room.',
          status: apiError.status,
        };
      }

      if (apiError.code === 'NOT_ASSIGNED_DOCTOR') {
        return {
          title: 'Not assigned as doctor',
          message: apiError.message || 'Participant is not assigned as doctor',
          suggestion: 'Ensure your participant name matches the doctor name for this consultation.',
          status: apiError.status,
        };
      }

      if (apiError.code === 'NOT_ASSIGNED_PATIENT') {
        return {
          title: 'Not assigned as patient',
          message: apiError.message || 'Participant is not assigned as patient',
          suggestion: 'Ensure your participant name matches the patient name for this consultation.',
          status: apiError.status,
        };
      }

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

type NoticeCardProps = {
  notice: ErrorNotice;
  kind: 'error' | 'info';
  onDismiss?: () => void;
  action?: {
    label: string;
    onClick: () => void;
    disabled?: boolean;
  };
};

type CreateConsultationPanelProps = {
  doctorName: string;
  patientName: string;
  busy: boolean;
  onDoctorNameChange: (value: string) => void;
  onPatientNameChange: (value: string) => void;
  onSubmit: () => void;
};

type JoinConsultationPanelProps = {
  consultationId: string;
  participantName: string;
  role: Role;
  onConsultationIdChange: (value: string) => void;
  onParticipantNameChange: (value: string) => void;
  onRoleChange: (value: Role) => void;
  onSubmit: () => void;
};

type CallViewProps = {
  joinState: JoinState;
  consultationExpiresAt: string | null;
  busy: boolean;
  consultationId: string;
  doctorName: string;
  locked: boolean;
  participants: ParticipantInfo[];
  onRequestJoinToken: (request?: Pick<JoinState, 'consultationId' | 'participantName' | 'role'>) => Promise<void>;
  onEndConsultation: () => void;
  onLeaveCall: () => void;
  onReturnToJoinForm: (errorNotice?: ErrorNotice) => void;
  onStageChange: (stage: 'preview' | 'connecting' | 'call' | null) => void;
  onLockConsultation: () => Promise<void>;
  onUnlockConsultation: () => Promise<void>;
  onListParticipants: () => Promise<void>;
  onRemoveParticipant: (identity: string) => Promise<void>;
  onMuteParticipant: (identity: string) => Promise<void>;
  onLoadChatHistory: (consultationId: string) => Promise<ChatMessageResponse[]>;
  onSendChatMessage: (consultationId: string, body: string) => Promise<void>;
};

type ConsultationController = {
  doctorName: string;
  patientName: string;
  participantName: string;
  role: Role;
  consultationId: string;
  consultation: Consultation | null;
  joinState: JoinState | null;
  status: string;
  errorNotice: ErrorNotice | null;
  sessionNotice: ErrorNotice | null;
  busy: boolean;
  waitingForAdmission: boolean;
  waitingRoomStatus: 'waiting' | 'admitted' | 'denied' | null;
  locked: boolean;
  participants: ParticipantInfo[];
  setCallStage: (stage: 'preview' | 'connecting' | 'call' | null) => void;
  setDoctorName: (value: string) => void;
  setPatientName: (value: string) => void;
  setParticipantName: (value: string) => void;
  setRole: (value: Role) => void;
  setConsultationId: (value: string) => void;
  setErrorNotice: (value: ErrorNotice | null) => void;
  setSessionNotice: (value: ErrorNotice | null) => void;
  createConsultation: () => Promise<void>;
  beginJoinSession: () => Promise<void>;
  joinConsultation: () => Promise<void>;
  endConsultation: () => Promise<void>;
  leaveCall: () => void;
  returnToJoinForm: () => void;
  cancelWaiting: () => void;
  lockConsultation: () => Promise<void>;
  unlockConsultation: () => Promise<void>;
  listParticipants: () => Promise<void>;
  removeParticipant: (identity: string) => Promise<void>;
  muteParticipant: (identity: string) => Promise<void>;
  loadChatHistory: (id: string) => Promise<ChatMessageResponse[]>;
  sendChatMessage: (id: string, body: string) => Promise<void>;
};

const formatCountdown = (totalSeconds: number): string => {
  const safeSeconds = Math.max(0, totalSeconds);
  const minutes = Math.floor(safeSeconds / 60);
  const seconds = safeSeconds % 60;

  return `${minutes}:${String(seconds).padStart(2, '0')}`;
};

const isTokenConnectionError = (error: unknown): boolean => {
  if (!error || typeof error !== 'object') {
    return false;
  }

  const livekitError = error as {
    name?: unknown;
    reasonName?: unknown;
    status?: unknown;
  };

  return (
    livekitError.name === 'ConnectionError'
    && livekitError.reasonName === 'NotAllowed'
    && (livekitError.status === 401 || livekitError.status === 403)
  );
};

const buildConsultationEndedNotice = (stage: 'preview' | 'connecting' | 'call' | null): ErrorNotice => {
  if (stage === 'call') {
    return {
      title: 'Consultation expired during the call',
      message: 'This consultation reached its scheduled end time while the room was active.',
      suggestion: 'Create a new consultation to continue.',
    };
  }

  if (stage === 'connecting') {
    return {
      title: 'Consultation expired before you connected',
      message: 'This consultation\'s time window ended while you were connecting to the room.',
      suggestion: 'Create a new consultation to continue.',
    };
  }

  return {
    title: 'Consultation expired during setup',
    message: 'This consultation\'s time window ended while you were setting up your camera and microphone.',
    suggestion: 'Create a new consultation to continue.',
  };
};

function useConsultation(): ConsultationController {
  const [doctorName, setDoctorName] = useState('Dr. Tachafy');
  const [patientName, setPatientName] = useState('Patient Demo');
  const [participantName, setParticipantName] = useState('Dr. Tachafy');
  const [role, setRole] = useState<Role>('doctor');
  const [consultationId, setConsultationId] = useState('');
  const [consultation, setConsultation] = useState<Consultation | null>(null);
  const [joinState, setJoinState] = useState<JoinState | null>(null);
  const [callStage, setCallStage] = useState<'preview' | 'connecting' | 'call' | null>(null);
  const [status, setStatus] = useState('Create a consultation, then join it from one or two browser windows.');
  const [errorNotice, setErrorNotice] = useState<ErrorNotice | null>(null);
  const [sessionNotice, setSessionNotice] = useState<ErrorNotice | null>(null);
  const [busy, setBusy] = useState(false);
  const [waitingForAdmission, setWaitingForAdmission] = useState(false);
  const [waitingRoomStatus, setWaitingRoomStatus] = useState<'waiting' | 'admitted' | 'denied' | null>(null);
  const [locked, setLocked] = useState(false);
  const [participants, setParticipants] = useState<ParticipantInfo[]>([]);

  const markConsultationEnded = useCallback((endedAt: string, notice: ErrorNotice) => {
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
    setCallStage(null);

    setJoinState((current) => (current === null ? current : null));
  }, []);

  const requestJson = useCallback(async <T,>(url: string, init?: RequestInit): Promise<T> => {
    const response = await fetch(url, {
      headers: { 'Content-Type': 'application/json' },
      ...init,
    });

    if (!response.ok) {
      throw await parseApiError(response);
    }

    return response.json() as Promise<T>;
  }, []);

  const cancelWaiting = useCallback(() => {
    setWaitingForAdmission(false);
    setWaitingRoomStatus(null);
    setJoinState(null);
    setCallStage(null);
    setStatus('Join the consultation again from the form.');
  }, []);

  const beginJoinSession = useCallback(async () => {
    const id = consultationId.trim();

    if (!id) {
      setErrorNotice({
        title: 'Consultation ID required',
        message: 'Paste a consultation ID before continuing to device check.',
        suggestion: 'Use the ID from the Create consultation panel.',
      });
      return;
    }

    setBusy(true);
    setErrorNotice(null);
    setSessionNotice(null);
    setCallStage(null);
    setWaitingForAdmission(false);
    setWaitingRoomStatus(null);

    try {
      const validateResponse = await requestJson<{
        consultation_id: string;
        room_name: string;
        participant_name: string;
        role: Role;
        expires_at: string;
        status: 'active' | 'ended';
      }>(`${API_URL}/api/consultations/${encodeURIComponent(id)}/validate`, {
        method: 'POST',
        body: JSON.stringify({
          participant_name: participantName,
          role,
        }),
      });

      // For non-doctor roles, register in the waiting room first.
      if (role !== 'doctor') {
        const wrResponse = await requestJson<WaitingRoomEntryData>(
          `${API_URL}/api/consultations/${encodeURIComponent(id)}/waiting-room/request`,
          {
            method: 'POST',
            body: JSON.stringify({
              participant_name: participantName,
              role,
            }),
          },
        );

        setWaitingRoomStatus(wrResponse.status);

        if (wrResponse.status === 'denied') {
          setWaitingForAdmission(true);
          setJoinState({
            consultationId: id,
            token: null,
            roomName: validateResponse.room_name,
            participantName: validateResponse.participant_name,
            role: validateResponse.role,
            expiresInSeconds: null,
            e2eeKey: null,
            tokenIssuedAt: null,
          });
          setStatus('Your request to join was denied by the doctor.');
          return;
        }

        if (wrResponse.status === 'waiting') {
          setWaitingForAdmission(true);
          setJoinState({
            consultationId: id,
            token: null,
            roomName: validateResponse.room_name,
            participantName: validateResponse.participant_name,
            role: validateResponse.role,
            expiresInSeconds: null,
            e2eeKey: null,
            tokenIssuedAt: null,
          });
          setStatus('Waiting for the doctor to admit you.');
          return;
        }

        // status === 'admitted' — fall through to normal join flow.
      }

      setJoinState({
        consultationId: id,
        token: null,
        roomName: validateResponse.room_name,
        participantName: validateResponse.participant_name,
        role: validateResponse.role,
        expiresInSeconds: null,
        e2eeKey: null,
        tokenIssuedAt: null,
      });
      setStatus('Device check opened. Pick your camera and microphone before joining the room.');
    } catch (e) {
      setJoinState(null);
      setWaitingForAdmission(false);
      setWaitingRoomStatus(null);
      setErrorNotice(createErrorNotice(e, 'Validating the consultation'));
      setStatus('Unable to validate the consultation. Review the details and try again.');
    } finally {
      setBusy(false);
    }
  }, [consultationId, participantName, requestJson, role]);

  const leaveCall = useCallback(() => {
    setJoinState(null);
    setCallStage(null);
    setWaitingForAdmission(false);
    setWaitingRoomStatus(null);
    setStatus('Call ended. Request a fresh token to rejoin if the consultation is still active.');
  }, []);

  const returnToJoinForm = useCallback((errorNotice?: ErrorNotice) => {
    setJoinState(null);
    setCallStage(null);
    setWaitingForAdmission(false);
    setWaitingRoomStatus(null);
    setStatus('Join the consultation again from the form.');
    if (errorNotice) {
      setErrorNotice(errorNotice);
    }
  }, []);

  const lockConsultation = useCallback(async () => {
    if (!joinState || !consultationId.trim()) {
      return;
    }

    setBusy(true);
    setErrorNotice(null);

    try {
      await requestJson<{ consultation_id: string; locked: boolean }>(
        `${API_URL}/api/consultations/${encodeURIComponent(consultationId.trim())}/lock`,
        {
          method: 'POST',
          body: JSON.stringify({
            participant_name: joinState.participantName,
            role: joinState.role,
          }),
        },
      );

      setLocked(true);
    } catch (e) {
      setErrorNotice(createErrorNotice(e, 'Locking the consultation'));
    } finally {
      setBusy(false);
    }
  }, [consultationId, joinState, requestJson]);

  const unlockConsultation = useCallback(async () => {
    if (!joinState || !consultationId.trim()) {
      return;
    }

    setBusy(true);
    setErrorNotice(null);

    try {
      await requestJson<{ consultation_id: string; locked: boolean }>(
        `${API_URL}/api/consultations/${encodeURIComponent(consultationId.trim())}/unlock`,
        {
          method: 'POST',
          body: JSON.stringify({
            participant_name: joinState.participantName,
            role: joinState.role,
          }),
        },
      );

      setLocked(false);
    } catch (e) {
      setErrorNotice(createErrorNotice(e, 'Unlocking the consultation'));
    } finally {
      setBusy(false);
    }
  }, [consultationId, joinState, requestJson]);

  const listParticipants = useCallback(async () => {
    if (!joinState || !consultationId.trim()) {
      return;
    }

    try {
      const response = await requestJson<ParticipantInfo[]>(
        `${API_URL}/api/consultations/${encodeURIComponent(consultationId.trim())}/participants`,
        {
          method: 'POST',
          body: JSON.stringify({
            participant_name: joinState.participantName,
            role: joinState.role,
          }),
        },
      );

      setParticipants(response);
    } catch (e) {
      // Silently fail on participant list errors
    }
  }, [consultationId, joinState, requestJson]);

  const removeParticipant = useCallback(async (identity: string) => {
    if (!joinState || !consultationId.trim()) {
      return;
    }

    setBusy(true);
    setErrorNotice(null);

    try {
      await requestJson<{ status: string }>(
        `${API_URL}/api/consultations/${encodeURIComponent(consultationId.trim())}/participants/${encodeURIComponent(identity)}/remove`,
        {
          method: 'POST',
          body: JSON.stringify({
            participant_name: joinState.participantName,
            role: joinState.role,
          }),
        },
      );

      // Refresh participant list
      void listParticipants();
    } catch (e) {
      setErrorNotice(createErrorNotice(e, 'Removing participant'));
    } finally {
      setBusy(false);
    }
  }, [consultationId, joinState, requestJson, listParticipants]);

  const muteParticipant = useCallback(async (identity: string) => {
    if (!joinState || !consultationId.trim()) {
      return;
    }

    setBusy(true);
    setErrorNotice(null);

    try {
      await requestJson<{ status: string; tracks_muted: number }>(
        `${API_URL}/api/consultations/${encodeURIComponent(consultationId.trim())}/participants/${encodeURIComponent(identity)}/mute`,
        {
          method: 'POST',
          body: JSON.stringify({
            participant_name: joinState.participantName,
            role: joinState.role,
          }),
        },
      );

      // Refresh participant list
      void listParticipants();
    } catch (e) {
      setErrorNotice(createErrorNotice(e, 'Muting participant'));
    } finally {
      setBusy(false);
    }
  }, [consultationId, joinState, requestJson, listParticipants]);

  const loadChatHistory = useCallback(async (id: string): Promise<ChatMessageResponse[]> => {
    try {
      const response = await requestJson<ChatMessageResponse[]>(
        `${API_URL}/api/consultations/${encodeURIComponent(id.trim())}/chat`,
        {
          method: 'GET',
        },
      );
      return response;
    } catch (e) {
      console.error('Failed to load chat history:', e);
      return [];
    }
  }, [requestJson]);

  const sendChatMessage = useCallback(async (id: string, body: string) => {
    if (!joinState || !id.trim()) {
      return;
    }

    try {
      await requestJson<ChatMessageResponse>(
        `${API_URL}/api/consultations/${encodeURIComponent(id.trim())}/chat`,
        {
          method: 'POST',
          body: JSON.stringify({
            participant_name: joinState.participantName,
            role: joinState.role,
            body,
          }),
        },
      );
    } catch (e) {
      console.error('Failed to send chat message:', e);
    }
  }, [joinState, requestJson]);

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
        ...buildConsultationEndedNotice(callStage),
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
  }, [consultation, callStage]);

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
            message: callStage === 'call'
              ? `This consultation was ended by ${latestMatchingEvent.ended_by ?? 'another window'} while the call was active.`
              : `This consultation was ended by ${latestMatchingEvent.ended_by ?? 'another window'} while you were still setting up.`,
            suggestion: 'Request a new consultation ID if you need to rejoin.',
          });
          return;
        }

        markConsultationEnded(consultation.expires_at, {
          ...buildConsultationEndedNotice(callStage),
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
  }, [consultation?.consultation_id, consultation?.expires_at, consultation?.status, callStage, joinState]);

  const endConsultation = useCallback(async () => {
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
  }, [consultationId, joinState, requestJson]);

  const createConsultation = useCallback(async () => {
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
  }, [doctorName, patientName, requestJson]);

  const joinConsultation = useCallback(async (request?: Pick<JoinState, 'consultationId' | 'participantName' | 'role'>) => {
    const joinDetails = request ?? joinState;

    if (!joinDetails) {
      return;
    }

    const id = joinDetails.consultationId.trim();

    if (!id) {
      return;
    }

    setBusy(true);

    try {
      const tokenResponse = await requestJson<{
        token: string;
        room_name: string;
        participant_name: string;
        role: Role;
        expires_in_seconds: number;
        e2ee_key: string;
      }>(`${API_URL}/api/consultations/${encodeURIComponent(id)}/token`, {
        method: 'POST',
        body: JSON.stringify({ participant_name: joinDetails.participantName, role: joinDetails.role }),
      });

      setJoinState({
        consultationId: id,
        token: tokenResponse.token,
        roomName: tokenResponse.room_name,
        participantName: tokenResponse.participant_name,
        role: tokenResponse.role,
        expiresInSeconds: tokenResponse.expires_in_seconds,
        e2eeKey: tokenResponse.e2ee_key,
        tokenIssuedAt: new Date().toISOString(),
      });
    } catch (e) {
      throw e;
    } finally {
      setBusy(false);
    }
  }, [joinState, requestJson]);

  return {
    doctorName,
    patientName,
    participantName,
    role,
    consultationId,
    consultation,
    joinState,
    status,
    errorNotice,
    sessionNotice,
    busy,
    waitingForAdmission,
    waitingRoomStatus,
    locked,
    participants,
    setCallStage,
    setDoctorName,
    setPatientName,
    setParticipantName,
    setRole,
    setConsultationId,
    setErrorNotice,
    setSessionNotice,
    createConsultation,
    beginJoinSession,
    joinConsultation,
    endConsultation,
    leaveCall,
    returnToJoinForm,
    cancelWaiting,
    lockConsultation,
    unlockConsultation,
    listParticipants,
    removeParticipant,
    muteParticipant,
    loadChatHistory,
    sendChatMessage,
  };
}

function BrandBar({ dark = false }: { dark?: boolean }) {
  return (
    <div className={dark ? 'brand-bar brand-bar--dark' : 'brand-bar'}>
      <div className="wordmark">tachafy</div>
      <span className="wordmark-tag">Teleconsultation</span>
    </div>
  );
}

function NoticeCard({ notice, kind, onDismiss, action }: NoticeCardProps) {
  if (kind === 'error') {
    return (
      <div className="notice-card" role="alert" aria-live="polite">
        <div className="notice-copy">
          <p className="notice-title">{notice.title}</p>
          <p className="notice-message">{notice.message}</p>
          <p className="notice-suggestion">{notice.suggestion}</p>
        </div>
        <div className="notice-actions">
          {action && (
            <button type="button" className="ghost-button" onClick={action.onClick} disabled={action.disabled}>
              {action.label}
            </button>
          )}
          {onDismiss && (
            <button type="button" className="ghost-button" onClick={onDismiss}>
              Dismiss
            </button>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="notice-card notice-card--info" aria-live="polite">
      <div className="notice-copy">
        <p className="notice-title">{notice.title}</p>
        <p className="notice-message">{notice.message}</p>
        <p className="notice-suggestion">{notice.suggestion}</p>
      </div>
      <div className="notice-actions">
        {action && (
          <button type="button" className="ghost-button" onClick={action.onClick} disabled={action.disabled}>
            {action.label}
          </button>
        )}
        {onDismiss && (
          <button type="button" className="ghost-button" onClick={onDismiss}>
            Dismiss
          </button>
        )}
      </div>
    </div>
  );
}

function CreateConsultationPanel({
  doctorName,
  patientName,
  busy,
  onDoctorNameChange,
  onPatientNameChange,
  onSubmit,
}: CreateConsultationPanelProps) {
  return (
    <form className="panel" onSubmit={(event) => { event.preventDefault(); onSubmit(); }}>
      <div className="panel-heading">
        <span>1</span>
        <h2>Create consultation</h2>
      </div>
      <label>
        Doctor name
        <input value={doctorName} onChange={(event) => onDoctorNameChange(event.target.value)} />
      </label>
      <label>
        Patient name
        <input value={patientName} onChange={(event) => onPatientNameChange(event.target.value)} />
      </label>
      <button type="submit" disabled={busy}>{busy ? 'Working...' : 'Create secure room'}</button>
    </form>
  );
}

function JoinConsultationPanel({
  consultationId,
  participantName,
  role,
  onConsultationIdChange,
  onParticipantNameChange,
  onRoleChange,
  onSubmit,
}: JoinConsultationPanelProps) {
  return (
    <form className="panel" onSubmit={(event) => { event.preventDefault(); onSubmit(); }}>
      <div className="panel-heading">
        <span>2</span>
        <h2>Join with role</h2>
      </div>
      <label>
        Consultation ID
        <input value={consultationId} onChange={(event) => onConsultationIdChange(event.target.value)} placeholder="Paste ID here" />
      </label>
      <label>
        Participant display name
        <input value={participantName} onChange={(event) => onParticipantNameChange(event.target.value)} />
      </label>
      <label>
        Role
        <select value={role} onChange={(event) => onRoleChange(event.target.value as Role)}>
          <option value="doctor">Doctor: publish + subscribe</option>
          <option value="patient">Patient: publish + subscribe</option>
          <option value="observer">Observer: subscribe only</option>
        </select>
      </label>
      <button type="submit">Continue to device check</button>
    </form>
  );
}

type DeviceChoices = {
  username: string;
  videoEnabled: boolean;
  audioEnabled: boolean;
  videoDeviceId: string;
  audioDeviceId: string;
};

function WaitingRoomScreen({
  consultationId,
  participantName,
  role,
  waitingRoomStatus,
  onCancel,
  onAdmitted,
}: {
  consultationId: string;
  participantName: string;
  role: Role;
  waitingRoomStatus: 'waiting' | 'admitted' | 'denied' | null;
  onCancel: () => void;
  onAdmitted: () => void;
}) {
  useEffect(() => {
    if (waitingRoomStatus !== 'waiting') {
      return;
    }

    let cancelled = false;

    const pollStatus = async () => {
      try {
        const response = await fetch(
          `${API_URL}/api/consultations/${encodeURIComponent(consultationId)}/waiting-room/request`,
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ participant_name: participantName, role }),
          },
        );

        if (!response.ok || cancelled) {
          return;
        }

        const entry = (await response.json()) as WaitingRoomEntryData;

        if (cancelled) {
          return;
        }

        if (entry.status === 'admitted') {
          onAdmitted();
        }
      } catch {
        // Silently retry on next interval.
      }
    };

    const intervalId = window.setInterval(pollStatus, 3000);

    return () => {
      cancelled = true;
      window.clearInterval(intervalId);
    };
  }, [consultationId, participantName, role, waitingRoomStatus, onAdmitted]);

  return (
    <div className="call-shell">
      <BrandBar dark />
      <div className="waiting-room-screen">
        {waitingRoomStatus === 'denied' ? (
          <>
            <p className="waiting-room-heading">Access denied</p>
            <p className="waiting-room-subtext waiting-room-denied">
              The doctor has denied your request to join this consultation.
            </p>
            <button type="button" className="waiting-room-back" onClick={onCancel}>
              Back to join form
            </button>
          </>
        ) : (
          <>
            <span className="spinner" aria-hidden="true" />
            <p className="waiting-room-heading">Waiting for the doctor to admit you</p>
            <p className="waiting-room-subtext">
              The doctor has not joined yet or hasn't admitted you. You'll be moved to the device check automatically once approved.
            </p>
            <button type="button" className="waiting-room-back" onClick={onCancel}>
              Cancel
            </button>
          </>
        )}
      </div>
    </div>
  );
}

function WaitingRoomPanel({
  consultationId,
  doctorName,
}: {
  consultationId: string;
  doctorName: string;
}) {
  const [entries, setEntries] = useState<WaitingRoomEntryData[]>([]);
  const [actionBusy, setActionBusy] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    const poll = async () => {
      try {
        const response = await fetch(
          `${API_URL}/api/consultations/${encodeURIComponent(consultationId)}/waiting-room`,
        );

        if (!response.ok || cancelled) {
          return;
        }

        const data = (await response.json()) as WaitingRoomEntryData[];

        if (!cancelled) {
          setEntries(data);
        }
      } catch {
        // Silently retry on next interval.
      }
    };

    void poll();
    const intervalId = window.setInterval(poll, 4000);

    return () => {
      cancelled = true;
      window.clearInterval(intervalId);
    };
  }, [consultationId]);

  const handleAction = useCallback(
    async (participantName: string, action: 'admit' | 'deny') => {
      setActionBusy(participantName);

      try {
        const response = await fetch(
          `${API_URL}/api/consultations/${encodeURIComponent(consultationId)}/waiting-room/${encodeURIComponent(participantName)}/${action}`,
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              actor_name: doctorName,
              actor_role: 'doctor',
            }),
          },
        );

        if (response.ok) {
          setEntries((current) =>
            current.filter((e) => e.participant_name !== participantName),
          );
        }
      } catch {
        // Will retry on next poll.
      } finally {
        setActionBusy(null);
      }
    },
    [consultationId, doctorName],
  );

  if (entries.length === 0) {
    return null;
  }

  return (
    <div className="waiting-room-panel">
      <div className="waiting-room-panel-header">
        <h3>Waiting room</h3>
        <span className="waiting-badge">{entries.length}</span>
      </div>
      <div className="waiting-room-list">
        {entries.map((entry) => (
          <div key={entry.participant_name} className="waiting-room-entry">
            <div className="waiting-room-entry-info">
              <span className="waiting-room-entry-name">{entry.participant_name}</span>
              <span className="waiting-room-entry-role">{entry.role}</span>
            </div>
            <div className="waiting-room-entry-actions">
              <button
                type="button"
                className="admit-button"
                disabled={actionBusy === entry.participant_name}
                onClick={() => void handleAction(entry.participant_name, 'admit')}
              >
                Admit
              </button>
              <button
                type="button"
                className="deny-button"
                disabled={actionBusy === entry.participant_name}
                onClick={() => void handleAction(entry.participant_name, 'deny')}
              >
                Deny
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function ParticipantsPanel({
  participants,
  onRemoveParticipant,
  onMuteParticipant,
}: {
  participants: ParticipantInfo[];
  onRemoveParticipant: (identity: string) => Promise<void>;
  onMuteParticipant: (identity: string) => Promise<void>;
}) {
  if (participants.length === 0) {
    return null;
  }

  return (
    <div className="participants-panel">
      <div className="participants-panel-header">
        <h3>Participants</h3>
        <span className="participants-badge">{participants.length}</span>
      </div>
      <div className="participants-list">
        {participants.map((participant) => (
          <div key={participant.identity} className="participant-entry">
            <div className="participant-entry-info">
              <span className="participant-entry-name">{participant.name || participant.identity}</span>
              <span className="participant-entry-role">{participant.role || 'unknown'}</span>
              <span className={`participant-entry-state ${participant.state || ''}`}>
                {participant.state || 'unknown'}
              </span>
            </div>
            <div className="participant-entry-actions">
              <button
                type="button"
                className="mute-button"
                onClick={() => void onMuteParticipant(participant.identity)}
                title="Mute participant"
                aria-label="Mute participant"
              >
                <MicOff size={14} strokeWidth={2.25} />
              </button>
              <button
                type="button"
                className="remove-button"
                onClick={() => void onRemoveParticipant(participant.identity)}
                title="Remove participant"
                aria-label="Remove participant"
              >
                <UserX size={14} strokeWidth={2.25} />
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function CustomVideoGrid() {
  const tracks = useTracks([
    { source: Track.Source.Camera, withPlaceholder: true },
    { source: Track.Source.ScreenShare, withPlaceholder: false },
  ]);

  return (
    <div className="custom-video-grid-wrap">
      <GridLayout tracks={tracks}>
        <ParticipantTile />
      </GridLayout>
    </div>
  );
}

function CustomChat({
  consultationId,
  onLoadChatHistory,
  onSendChatMessage,
}: {
  consultationId: string;
  onLoadChatHistory: (consultationId: string) => Promise<ChatMessageResponse[]>;
  onSendChatMessage: (consultationId: string, body: string) => Promise<void>;
}) {
  const chat = useChat();
  const [historyMessages, setHistoryMessages] = useState<ChatMessageResponse[]>([]);
  const [historyLoaded, setHistoryLoaded] = useState(false);
  const [loading, setLoading] = useState(true);
  const [draft, setDraft] = useState('');
  const [sending, setSending] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  // Load history on mount
  useEffect(() => {
    if (historyLoaded) {
      return;
    }

    void onLoadChatHistory(consultationId)
      .then((messages) => {
        setHistoryMessages(messages);
        setHistoryLoaded(true);
        setLoading(false);
      })
      .catch((error) => {
        console.error('Failed to load chat history:', error);
        setHistoryLoaded(true);
        setLoading(false);
      });
  }, [consultationId, historyLoaded, onLoadChatHistory]);

  // Auto-scroll to bottom on new messages
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [chat.chatMessages.length, historyMessages.length]);

  const sendMessage = async () => {
    const body = draft.trim();
    if (!body || sending) return;
    setSending(true);
    try {
      // Send via LiveKit for real-time delivery
      await chat.send(body);
      // Persist to backend
      await onSendChatMessage(consultationId, body);
      setDraft('');
    } catch (e) {
      console.error('Failed to send chat message', e);
    } finally {
      setSending(false);
    }
  };

  // Merge historical and live messages
  const combined = [
    ...historyMessages.map((m) => ({
      id: `history-${m.sent_at}-${m.sender_identity}`,
      from: m.sender_name,
      message: m.body,
      timestamp: new Date(m.sent_at).getTime(),
      isLocal: false, // Historical messages are from others
    })),
    ...chat.chatMessages.map((m) => ({
      id: m.id,
      from: m.from?.name ?? m.from?.identity ?? 'Unknown',
      message: m.message,
      timestamp: m.timestamp,
      isLocal: m.from?.isLocal ?? false,
    })),
  ].sort((a, b) => a.timestamp - b.timestamp);

  return (
    <div className="custom-chat">
      <div className="custom-chat-header">
        <h3>Chat</h3>
      </div>
      <div className="custom-chat-messages">
        {loading ? (
          <div className="custom-chat-loading">Loading messages...</div>
        ) : combined.length === 0 ? (
          <div className="custom-chat-empty">No messages yet</div>
        ) : (
          combined.map((m) => (
            <div
              key={m.id}
              className={`custom-chat-message ${m.isLocal ? 'custom-chat-message-local' : ''}`}
            >
              <span className="custom-chat-message-sender">{m.from}</span>
              <span className="custom-chat-message-body">{m.message}</span>
            </div>
          ))
        )}
        <div ref={messagesEndRef} />
      </div>
      <div className="custom-chat-input">
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && void sendMessage()}
          placeholder="Type a message…"
          disabled={sending}
        />
        <button
          type="button"
          onClick={() => void sendMessage()}
          disabled={sending || !draft.trim()}
        >
          {sending ? 'Sending...' : 'Send'}
        </button>
      </div>
    </div>
  );
}

function CallView({
  joinState,
  consultationExpiresAt,
  busy,
  consultationId,
  doctorName,
  locked,
  participants,
  onRequestJoinToken,
  onEndConsultation,
  onLeaveCall,
  onReturnToJoinForm,
  onStageChange,
  onLockConsultation,
  onUnlockConsultation,
  onListParticipants,
  onRemoveParticipant,
  onMuteParticipant,
  onLoadChatHistory,
  onSendChatMessage,
}: CallViewProps) {
  const { room, keyProvider } = useMemo(() => {
    const keyProvider = new ExternalE2EEKeyProvider();
    const worker = new Worker(new URL('livekit-client/e2ee-worker', import.meta.url), {
      type: 'module',
    });

    return {
      keyProvider,
      room: new Room({
        encryption: {
          keyProvider,
          worker,
        },
      }),
    };
  }, []);

  const skipPreview = joinState.role === 'observer';
  const [stage, setStage] = useState<'preview' | 'connecting' | 'call'>(skipPreview ? 'connecting' : 'preview');
  const [deviceChoices, setDeviceChoices] = useState<DeviceChoices | null>(null);
  const [connectionNotice, setConnectionNotice] = useState<ErrorNotice | null>(null);
  const [connectionAction, setConnectionAction] = useState<null | (() => void)>(null);
  const [now, setNow] = useState(() => Date.now());
  const [tokenRequestPending, setTokenRequestPending] = useState(false);
  const observerTokenRequested = useRef(false);
  const readyToConnect = skipPreview || deviceChoices !== null;
  const onRequestJoinTokenRef = useRef(onRequestJoinToken);

  useEffect(() => {
    onRequestJoinTokenRef.current = onRequestJoinToken;
  }, [onRequestJoinToken]);

  useEffect(() => {
    onStageChange(stage);
  }, [onStageChange, stage]);

  useEffect(() => () => onStageChange(null), [onStageChange]);

  // Poll participants when in call stage and role is doctor
  useEffect(() => {
    if (stage !== 'call' || joinState.role !== 'doctor') {
      return;
    }

    void onListParticipants();
    const intervalId = window.setInterval(() => {
      void onListParticipants();
    }, 5000);

    return () => window.clearInterval(intervalId);
  }, [stage, joinState.role, onListParticipants]);

  // Load chat history when entering call stage
  useEffect(() => {
    if (stage !== 'call' || !consultationId) {
      return;
    }

    void onLoadChatHistory(consultationId).catch((error) => {
      console.error('Failed to load chat history:', error);
    });
  }, [stage, consultationId, onLoadChatHistory]);

  useEffect(() => {
    if (!joinState.token || !joinState.expiresInSeconds || !joinState.tokenIssuedAt) {
      return;
    }

    setNow(Date.now());
    const intervalId = window.setInterval(() => setNow(Date.now()), 1000);

    return () => window.clearInterval(intervalId);
  }, [joinState.expiresInSeconds, joinState.token, joinState.tokenIssuedAt]);

  useEffect(() => {
    if (stage !== 'preview' || !consultationExpiresAt) {
      return;
    }

    setNow(Date.now());
    const intervalId = window.setInterval(() => setNow(Date.now()), 1000);

    return () => window.clearInterval(intervalId);
  }, [stage, consultationExpiresAt]);

  const requestFreshToken = useCallback(async (request: Pick<JoinState, 'consultationId' | 'participantName' | 'role'>) => {
    setConnectionNotice(null);
    setConnectionAction(null);
    setTokenRequestPending(true);

    try {
      await onRequestJoinTokenRef.current(request);
    } catch (error) {
      const notice = createErrorNotice(error, 'Joining the consultation');
      const apiStatus = error instanceof Error && 'status' in error
        ? (error as ApiError).status
        : undefined;

      setConnectionNotice(notice);
      setConnectionAction(apiStatus === 404 || apiStatus === 409 || apiStatus === 410 || apiStatus === 403
        ? () => {
          setTimeout(() => onReturnToJoinForm(notice), 0);
        }
        : () => {
          setStage('connecting');
          void requestFreshToken(request);
        });
      setStage('preview');
    } finally {
      setTokenRequestPending(false);
    }
  }, [onReturnToJoinForm]);

  useEffect(() => {
    if (!skipPreview || observerTokenRequested.current || joinState.token || tokenRequestPending) {
      return;
    }

    observerTokenRequested.current = true;
    void requestFreshToken({
      consultationId: joinState.consultationId,
      participantName: joinState.participantName,
      role: joinState.role,
    });
  }, [joinState.consultationId, joinState.participantName, joinState.role, joinState.token, requestFreshToken, skipPreview, tokenRequestPending]);

  useEffect(() => {
    const token = joinState.token;
    const e2eeKey = joinState.e2eeKey;

    if (!readyToConnect || stage === 'preview' || !token || !e2eeKey) {
      return;
    }

    let cancelled = false;

    const connectRoom = async () => {
      setConnectionNotice(null);
      setConnectionAction(null);
      await keyProvider.setKey(e2eeKey);
      await room.setE2EEEnabled(true);

      if (cancelled) {
        return;
      }

      await room.connect(LIVEKIT_URL, token);

      if (cancelled) {
        return;
      }

      if (deviceChoices) {
        await room.localParticipant.setCameraEnabled(deviceChoices.videoEnabled, { deviceId: deviceChoices.videoDeviceId });
        await room.localParticipant.setMicrophoneEnabled(deviceChoices.audioEnabled, { deviceId: deviceChoices.audioDeviceId });
      }

      if (!cancelled) {
        setStage('call');
      }
    };

    void connectRoom().catch((error) => {
      if (!cancelled) {
        if (isTokenConnectionError(error)) {
          setConnectionNotice({
            title: 'Your session timed out before connecting',
            message: 'LiveKit rejected the token before the room joined.',
            suggestion: 'Request a fresh token and retry the connection.',
          });
          setConnectionAction(() => () => {
            setStage('connecting');
            void requestFreshToken({
              consultationId: joinState.consultationId,
              participantName: joinState.participantName,
              role: joinState.role,
            });
          });
        } else {
          const notice = createErrorNotice(error, 'Connecting to the consultation room');
          setConnectionNotice(notice);
          setConnectionAction(() => () => onReturnToJoinForm(notice));
        }

        setStage('preview');
        room.disconnect();
      }
    });

    return () => {
      cancelled = true;
    };
  }, [
    readyToConnect,
    skipPreview,
    stage,
    deviceChoices,
    joinState.e2eeKey,
    joinState.token,
    joinState.consultationId,
    joinState.participantName,
    joinState.role,
    keyProvider,
    requestFreshToken,
    onReturnToJoinForm,
    room,
  ]);

  const tokenExpiresAtMs = joinState.tokenIssuedAt && joinState.expiresInSeconds
    ? Date.parse(joinState.tokenIssuedAt) + joinState.expiresInSeconds * 1000
    : null;

  const tokenCountdown = tokenExpiresAtMs !== null
    ? formatCountdown(Math.ceil((tokenExpiresAtMs - now) / 1000))
    : null;

  const handlePreJoinSubmit = useCallback((choices: any) => {
    setConnectionNotice(null);
    setConnectionAction(null);
    setDeviceChoices({
      username: choices.username,
      videoEnabled: choices.videoEnabled,
      audioEnabled: choices.audioEnabled,
      videoDeviceId: choices.videoDeviceId,
      audioDeviceId: choices.audioDeviceId,
    });
    setTokenRequestPending(true);
    setStage('connecting');
    void requestFreshToken({
      consultationId: joinState.consultationId,
      participantName: choices.username,
      role: joinState.role,
    });
  }, [joinState.consultationId, joinState.role, requestFreshToken]);

  const handlePreJoinError = useCallback((error: Error) => {
    setConnectionNotice(createErrorNotice(error, 'Opening the device check'));
  }, []);

  const prejoinWidget = useMemo(() => (
    <div data-lk-theme="default" className="prejoin-widget">
      <PreJoin
        defaults={{ username: joinState.participantName }}
        joinLabel="Join consultation"
        micLabel="Microphone"
        camLabel="Camera"
        userLabel="Display name"
        persistUserChoices
        onSubmit={handlePreJoinSubmit}
        onError={handlePreJoinError}
      />
    </div>
  ), [joinState.participantName, handlePreJoinSubmit, handlePreJoinError]);

  const consultationCountdown = consultationExpiresAt
    ? formatCountdown(Math.ceil((Date.parse(consultationExpiresAt) - now) / 1000))
    : null;

  if (stage === 'preview') {
    return (
      <div className="call-shell">
        <BrandBar dark />
        <div className="prejoin-stage">
          <p className="prejoin-heading">Check your camera and microphone</p>
          <p className="prejoin-subheading">Everything here stays on this device until you join.</p>
          {consultationCountdown && (
            <p className="prejoin-countdown">This consultation closes in {consultationCountdown}.</p>
          )}
          {prejoinWidget}
          {connectionNotice && (
            <NoticeCard
              notice={connectionNotice}
              kind="info"
              onDismiss={connectionAction ? undefined : () => {
                setConnectionNotice(null);
                setConnectionAction(null);
              }}
              action={connectionAction
                ? {
                  label: connectionNotice.title === 'Consultation not found' || connectionNotice.title === 'Consultation no longer available' || connectionNotice.title === 'Access denied'
                    ? 'Back to join form'
                    : 'Retry with a fresh token',
                  onClick: connectionAction,
                  disabled: busy,
                }
                : undefined}
            />
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="call-shell">
      <BrandBar dark />
      <div className="call-strip">
        <div>
          <strong>{joinState.participantName}</strong>
          <span>
            {joinState.role}
            {' '}
            ·
            {' '}
            {joinState.roomName ?? 'Preparing room'}
            {tokenCountdown && (
              <>
                {' '}
                · token expires in {tokenCountdown}
              </>
            )}
          </span>
        </div>
        <div className="call-strip-actions">
          {joinState.role === 'doctor' && (
            <>
              <button 
                type="button" 
                className={`lock-button ${locked ? 'locked' : ''}`} 
                onClick={() => locked ? void onUnlockConsultation() : void onLockConsultation()} 
                disabled={busy}
              >
                {locked ? '🔒 Unlock' : '🔓 Lock'}
              </button>
              <button type="button" className="end-button" onClick={onEndConsultation} disabled={busy}>End consultation</button>
            </>
          )}
          <button type="button" className="leave-button" onClick={() => { room.disconnect(); onLeaveCall(); }}>Leave test</button>
        </div>
      </div>
      {joinState.role === 'doctor' && (
        <>
          <WaitingRoomPanel consultationId={consultationId} doctorName={doctorName} />
          <ParticipantsPanel 
            participants={participants} 
            onRemoveParticipant={onRemoveParticipant}
            onMuteParticipant={onMuteParticipant}
          />
        </>
      )}
      {connectionNotice && (
        <NoticeCard
          notice={connectionNotice}
          kind="info"
          onDismiss={connectionAction ? undefined : () => {
            setConnectionNotice(null);
            setConnectionAction(null);
          }}
          action={connectionAction
            ? {
              label: 'Retry with a fresh token',
              onClick: connectionAction,
              disabled: busy,
            }
            : undefined}
        />
      )}
      <div className="call-stage" style={{ height: 'calc(100dvh - 116px)' }}>
        {(stage === 'connecting' || tokenRequestPending) && !connectionNotice && (
          <div className="connecting-overlay" role="status" aria-live="polite">
            <span className="spinner" aria-hidden="true" />
            <p>
              {tokenCountdown
                ? `Connecting to the secure room. Token expires in ${tokenCountdown}.`
                : 'Connecting to the secure room…'}
            </p>
          </div>
        )}
        <LiveKitRoom
          room={room}
          serverUrl={undefined}
          token={undefined}
          video={false}
          audio={false}
          onDisconnected={onLeaveCall}
          data-lk-theme="default"
          style={{ height: '100%' }}
        >
          <div className="custom-call-layout">
            <div className="custom-call-video">
              <CustomVideoGrid />
              <ControlBar />
            </div>
            <div className="custom-call-chat">
              <CustomChat
                consultationId={consultationId}
                onLoadChatHistory={onLoadChatHistory}
                onSendChatMessage={onSendChatMessage}
              />
            </div>
          </div>
          <RoomAudioRenderer />
        </LiveKitRoom>
      </div>
    </div>
  );
}

function App() {
  const {
    doctorName,
    patientName,
    participantName,
    role,
    consultationId,
    consultation,
    joinState,
    status,
    errorNotice,
    sessionNotice,
    busy,
    waitingForAdmission,
    waitingRoomStatus,
    locked,
    participants,
    setCallStage,
    setDoctorName,
    setPatientName,
    setParticipantName,
    setRole,
    setConsultationId,
    setErrorNotice,
    setSessionNotice,
    createConsultation,
    beginJoinSession,
    joinConsultation,
    endConsultation,
    leaveCall,
    returnToJoinForm,
    cancelWaiting,
    lockConsultation,
    unlockConsultation,
    listParticipants,
    removeParticipant,
    muteParticipant,
    loadChatHistory,
    sendChatMessage,
  } = useConsultation();

  const handleWaitingRoomAdmitted = useCallback(() => {
    // Transition from waiting screen to the normal prejoin/device-check flow.
    // Re-run beginJoinSession which will now get status=admitted.
    void beginJoinSession();
  }, [beginJoinSession]);

  if (joinState !== null && waitingForAdmission) {
    return (
      <WaitingRoomScreen
        consultationId={joinState.consultationId}
        participantName={joinState.participantName}
        role={joinState.role}
        waitingRoomStatus={waitingRoomStatus}
        onCancel={cancelWaiting}
        onAdmitted={handleWaitingRoomAdmitted}
      />
    );
  }

  if (joinState !== null) {
    return (
      <CallView
        joinState={joinState}
        consultationExpiresAt={consultation?.expires_at ?? null}
        busy={busy}
        consultationId={consultationId}
        doctorName={doctorName}
        locked={locked}
        participants={participants}
        onRequestJoinToken={joinConsultation}
        onEndConsultation={endConsultation}
        onLeaveCall={leaveCall}
        onReturnToJoinForm={returnToJoinForm}
        onStageChange={setCallStage}
        onLockConsultation={lockConsultation}
        onUnlockConsultation={unlockConsultation}
        onListParticipants={listParticipants}
        onLoadChatHistory={loadChatHistory}
        onSendChatMessage={sendChatMessage}
        onRemoveParticipant={removeParticipant}
        onMuteParticipant={muteParticipant}
      />
    );
  }

  return (
    <main className="teleconsultation-console">
      <BrandBar />
      <section className="intro-panel">
        <p className="eyebrow">Tachafy LiveKit MVP</p>
        <h1>Teleconsultation access console</h1>
        <p>
          Create an isolated consultation room, issue short-lived role-based tokens, then join from two browser windows to test doctor/patient interaction.
        </p>
      </section>

      <section className="workflow-grid">
        <CreateConsultationPanel
          doctorName={doctorName}
          patientName={patientName}
          busy={busy}
          onDoctorNameChange={setDoctorName}
          onPatientNameChange={setPatientName}
          onSubmit={createConsultation}
        />

        <JoinConsultationPanel
          consultationId={consultationId}
          participantName={participantName}
          role={role}
          onConsultationIdChange={setConsultationId}
          onParticipantNameChange={setParticipantName}
          onRoleChange={setRole}
          onSubmit={beginJoinSession}
        />
      </section>

      <section className="status-panel">
        <h2>Session state</h2>
        {errorNotice && (
          <NoticeCard notice={errorNotice} kind="error" onDismiss={() => setErrorNotice(null)} />
        )}
        {!errorNotice && sessionNotice && (
          <NoticeCard notice={sessionNotice} kind="info" onDismiss={() => setSessionNotice(null)} />
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