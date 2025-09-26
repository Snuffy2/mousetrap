import React, {
  useEffect,
  useState,
  useRef,
  useCallback,
  forwardRef,
  useImperativeHandle,
} from 'react';
import {
  Card,
  CardContent,
  Typography,
  Box,
  Snackbar,
  Alert,
  Divider,
  Button,
  Tooltip,
} from '@mui/material';
import CheckCircleIcon from '@mui/icons-material/CheckCircle';
import CancelIcon from '@mui/icons-material/Cancel';
import InfoOutlinedIcon from '@mui/icons-material/InfoOutlined';
import { getStatusMessageColor } from '../utils/utils';
import { stringifyMessage } from '../utils/statusUtils';
import NetworkProxyDetailsAccordion from './NetworkProxyDetailsAccordion';
import MamDetailsAccordion from './MamDetailsAccordion';
import AutomationStatusRow from './AutomationStatusRow';
import TimerDisplay from './TimerDisplay';

import { useSession } from '../context/SessionContext';

/**
 * @typedef {Object} StatusCardProps
 * @property {boolean} [autoWedge]
 * @property {boolean} [autoVIP]
 * @property {boolean} [autoUpload]
 * @property {Function} [_onSessionSaved]
 * @property {Function} [_onSessionDataChanged]
 * @property {Function} [onStatusUpdate]
 */

const StatusCard = forwardRef(function StatusCard(
  /** @type {StatusCardProps} */ {
    autoWedge,
    autoVIP,
    autoUpload,
    _onSessionSaved,
    _onSessionDataChanged,
    onStatusUpdate,
  },
  ref
) {
  const { sessionLabel, setDetectedIp, setPoints, setCheese, status, setStatus } = useSession();
  const [timer, setTimer] = useState(0);
  /** @type {{open: boolean, message: any, severity: 'success'|'error'|'info'|'warning'}} */
  const initialSnackbar = {
    open: false,
    message: '',
    severity: 'info',
  };

  const [snackbar, setSnackbar] = useState(initialSnackbar);
  const [seedboxStatus, setSeedboxStatus] = useState(null);
  const [seedboxLoading, setSeedboxLoading] = useState(false);
  const pollingRef = useRef(null);
  const lastNextCheckRef = useRef(null);

  // Fetch status from backend
  const fetchStatus = useCallback(
    async (force = false) => {
      try {
        let url = sessionLabel
          ? `/api/status?label=${encodeURIComponent(sessionLabel)}`
          : '/api/status';
        if (force) url += (url.includes('?') ? '&' : '?') + 'force=1';
        const res = await fetch(url);
        const data = await res.json();
        if (data.success === false || data.error) {
          setStatus({
            error: data.error || 'Unknown error from backend.',
          });
          setSnackbar({
            open: true,
            message: stringifyMessage(data.error || 'Unknown error from backend.'),
            severity: 'error',
          });
          setPoints && setPoints(null);
          setCheese && setCheese(null);
          return;
        }
        const detectedIp = data.detected_public_ip || '';
        const newStatus = {
          last_update_mamid: data.mam_id || '',
          ratelimit: data.ratelimit || 0, // seconds, from backend
          check_freq: data.check_freq || 5, // minutes, from backend
          last_result: data.message || '',
          ip: detectedIp,
          current_ip: data.current_ip,
          current_ip_asn: data.current_ip_asn,
          detected_public_ip: data.detected_public_ip,
          detected_public_ip_asn: data.detected_public_ip_asn,
          detected_public_ip_as: data.detected_public_ip_as,
          proxied_public_ip: data.proxied_public_ip,
          proxied_public_ip_asn: data.proxied_public_ip_asn,
          proxied_public_ip_as: data.proxied_public_ip_as,
          mam_cookie_exists: data.mam_cookie_exists,
          mam_session_as: data.mam_session_as,
          asn: data.asn || '',
          last_check_time: data.last_check_time || null,
          next_check_time: data.next_check_time || null, // <-- NEW
          points: data.points || null,
          cheese: data.cheese || null,
          status_message: data.status_message || '', // user-friendly status message
          details: data.details || {}, // raw backend details
        };
        setStatus(newStatus);
        if (onStatusUpdate) onStatusUpdate(newStatus);
        setDetectedIp && setDetectedIp(detectedIp);
        setPoints && setPoints(data.points || null);
        setCheese && setCheese(data.cheese || null);
        return newStatus;
      } catch (e) {
        setStatus({ error: e.message || 'Failed to fetch status.' });
        setSnackbar({
          open: true,
          message: stringifyMessage(e.message || 'Failed to fetch status.'),
          severity: 'error',
        });
        setPoints && setPoints(null);
        setCheese && setCheese(null);
        return undefined;
      }
    },
    [sessionLabel, setDetectedIp, setPoints, setCheese, setStatus, onStatusUpdate]
  );

  // Poll backend every 5 seconds for status, but only if session is fully configured
  useEffect(() => {
    // Only poll when session is configured and next_check_time exists.
    const isConfigured = status && status.configured !== false && status.next_check_time;

    if (!isConfigured) return undefined;

    // If timer is not near zero, don't start aggressive polling.
    if (timer > 10) return undefined;

    // Initialize the last-next-check ref so we can stop polling when it changes.
    lastNextCheckRef.current = status.next_check_time || null;

    pollingRef.current = setInterval(async () => {
      const res = await fetchStatus(false);
      const newNextCheck = (res && res.next_check_time) || (status && status.next_check_time);
      if (newNextCheck && newNextCheck !== lastNextCheckRef.current) {
        // Next check moved — stop aggressive polling and let the timer take over.
        if (pollingRef.current) {
          clearInterval(pollingRef.current);
          pollingRef.current = null;
        }
      }
    }, 5000);

    return () => {
      if (pollingRef.current) {
        clearInterval(pollingRef.current);
        pollingRef.current = null;
      }
    };
  }, [timer, sessionLabel, status?.next_check_time, status?.configured, fetchStatus]);

  // Timer is always derived from backend's next_check_time and current time
  useEffect(() => {
    // Calculate timer immediately when next_check_time changes
    const calculateTimer = () => {
      if (status && status.next_check_time) {
        const nextCheck = Date.parse(status.next_check_time);
        const now = Date.now();
        let secondsLeft = Math.floor((nextCheck - now) / 1000);
        setTimer(Math.max(0, secondsLeft));
      } else {
        setTimer(0);
      }
    };

    // Run immediately
    calculateTimer();

    // Then run every second
    let interval = setInterval(calculateTimer, 1000);
    return () => clearInterval(interval);
  }, [status?.next_check_time]); // Use optional chaining and the actual string value

  // Always perform a force=1 status check on first load/session select
  // On initial load/session select, fetch latest status (do NOT force a backend check)
  useEffect(() => {
    if (!sessionLabel) return;
    setStatus(null); // Clear status to show loading/blank until check completes
    fetchStatus(false);
  }, [sessionLabel]);

  // Clear seedbox status when session changes
  useEffect(() => {
    setSeedboxStatus(null);
  }, [sessionLabel]);

  // Handler for 'Check Now' button
  const handleCheckNow = async () => {
    await fetchStatus(true);
    setSnackbar({
      open: true,
      message: 'Checked now!',
      severity: 'success',
    });
  };

  // Handler for 'Update Seedbox' button
  const handleUpdateSeedbox = async () => {
    if (!sessionLabel) return;
    setSeedboxLoading(true);
    setSeedboxStatus(null);
    try {
      const res = await fetch('/api/session/update_seedbox', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ label: sessionLabel }),
      });
      const data = await res.json();
      setSeedboxStatus(data);
      setSnackbar({
        open: true,
        message: data.success ? data.msg || 'Seedbox updated!' : data.error || 'Update failed',
        severity: data.success ? 'success' : 'warning',
      });
      fetchStatus(); // Refresh status after update
    } catch (e) {
      setSeedboxStatus({ success: false, error: e.message });
      setSnackbar({
        open: true,
        message: 'Seedbox update failed',
        severity: 'error',
      });
    } finally {
      setSeedboxLoading(false);
    }
  };

  // Expose a method for parent to force a status refresh (e.g., after session save)
  useImperativeHandle(ref, () => ({
    fetchStatus,
    forceStatusRefresh: handleCheckNow,
  }));

  return (
    <Card sx={{ mb: 3, borderRadius: 2 }}>
      <CardContent>
        {/* Session Status Header: align text and icon vertically with buttons */}
        <Box
          sx={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
          }}
        >
          <Box
            sx={{
              display: 'flex',
              alignItems: 'center',
              minHeight: 48,
            }}
          >
            <Box
              sx={{
                display: 'flex',
                alignItems: 'center',
                height: 40,
              }}
            >
              <Typography
                variant="h6"
                sx={{
                  display: 'flex',
                  alignItems: 'center',
                  height: 40,
                  mb: 0,
                  mr: 1,
                }}
              >
                Session Status
              </Typography>
              {/* Session Status Icon Logic (finalized) */}
              {(() => {
                if (!status || status.configured === false || !status.mam_cookie_exists)
                  return null;
                const details = status.details || {};
                // Show red X if last check was unsuccessful (error present, or success false)
                if (details.error || details.success === false) {
                  return (
                    <CancelIcon
                      sx={{
                        color: 'error.main',
                        fontSize: 28,
                        verticalAlign: 'middle',
                      }}
                      titleAccess="Session error"
                    />
                  );
                }
                // Show green check if last check was successful (no error, and success is true or missing/undefined/null)
                if (
                  !details.error &&
                  (details.success === true ||
                    details.success === undefined ||
                    details.success === null)
                ) {
                  return (
                    <CheckCircleIcon
                      sx={{
                        color: 'success.main',
                        fontSize: 28,
                        verticalAlign: 'middle',
                      }}
                      titleAccess="Session valid"
                    />
                  );
                }
                // Show yellow question mark if status is unknown (no error, no explicit success/failure)
                return (
                  <InfoOutlinedIcon
                    sx={{
                      color: 'warning.main',
                      fontSize: 28,
                      verticalAlign: 'middle',
                    }}
                    titleAccess="Session unknown"
                  />
                );
              })()}
            </Box>
          </Box>
          <Box>
            <Tooltip title="Refreshes session status from MAM">
              <span>
                <Button variant="outlined" size="small" onClick={handleCheckNow} sx={{ ml: 2 }}>
                  Check Now
                </Button>
              </span>
            </Tooltip>
            {/* USE DETECTED IP and USE DETECTED VPN IP buttons moved to MouseTrapConfigCard */}
            <Tooltip title="Updates your session's IP/ASN with MAM (rate-limited to once per hour)">
              <span>
                <Button
                  variant="contained"
                  size="small"
                  color="secondary"
                  onClick={handleUpdateSeedbox}
                  sx={{ ml: 2 }}
                  disabled={seedboxLoading || !sessionLabel}
                >
                  {seedboxLoading ? 'Updating...' : 'Update Seedbox'}
                </Button>
              </span>
            </Tooltip>
          </Box>
        </Box>
        {/* Network & Proxy Details Accordion */}
        <NetworkProxyDetailsAccordion status={status} />
        {/* MAM Details Accordion (restored, styled to match) */}
        {/* Robust error handling: if status is set and has error, only render the error alert */}
        {status && status.error ? (
          <Box sx={{ mt: 2, mb: 2 }}>
            <Alert severity="error">{status.error}</Alert>
          </Box>
        ) : status ? (
          status.configured === false ||
          (status.status_message &&
            status.status_message ===
              'Session not configured. Please save session details to begin.') ? (
            <Box sx={{ mt: 2, mb: 2 }}>
              <Alert severity="info">
                {status.status_message ||
                  'Session not configured. Please save session details to begin.'}
              </Alert>
            </Box>
          ) : (
            <Box>
              {/* Timer Display and Automation Status Row */}
              {status.last_check_time && (
                <React.Fragment>
                  <TimerDisplay timer={timer} />
                  <Typography
                    variant="body2"
                    sx={{
                      mt: 1,
                      textAlign: 'center',
                      fontWeight: 500,
                    }}
                  >
                    {/* Unified, styled status message */}
                    {(() => {
                      // Prefer rate limit message if present
                      let msg = status.status_message || status.last_result || 'unknown';
                      if (
                        typeof msg === 'string' &&
                        /rate limit: last change too recent/i.test(msg)
                      ) {
                        // msg = msg; // This line is redundant and can be removed
                      } else if (
                        status.details &&
                        typeof status.details === 'object' &&
                        status.details.error &&
                        /rate limit: last change too recent/i.test(status.details.error)
                      ) {
                        msg = status.details.error;
                      }
                      const color = getStatusMessageColor(msg);
                      return (
                        <Box
                          sx={{
                            mt: 1,
                            textAlign: 'center',
                          }}
                        >
                          <Typography
                            variant="subtitle2"
                            color={color}
                            sx={{
                              fontWeight: 600,
                              letterSpacing: 0.5,
                            }}
                          >
                            {msg}
                          </Typography>
                        </Box>
                      );
                    })()}
                  </Typography>
                  {/* Proxy/VPN error warning below timer/status */}
                  {status.proxy_error && (
                    <Box sx={{ mt: 2, mb: 1 }}>
                      <Alert severity="warning">{status.proxy_error}</Alert>
                    </Box>
                  )}
                  <AutomationStatusRow
                    autoWedge={autoWedge}
                    autoVIP={autoVIP}
                    autoUpload={autoUpload}
                  />
                  <MamDetailsAccordion status={status} />
                  {/* 1px invisible box for padding/squared corners below MAM Details accordion */}
                  <Box
                    sx={{
                      height: 1,
                      width: '100%',
                      border: 0,
                      background: 'none',
                      p: 0,
                      m: 0,
                    }}
                  />
                </React.Fragment>
              )}
            </Box>
          )
        ) : (
          <Typography color="error">Status unavailable</Typography>
        )}
        <Snackbar
          open={snackbar.open}
          autoHideDuration={2000}
          onClose={() => setSnackbar((s) => ({ ...s, open: false }))}
        >
          <Alert
            onClose={() => setSnackbar((s) => ({ ...s, open: false }))}
            severity={snackbar.severity}
            sx={{ width: '100%' }}
          >
            {stringifyMessage(snackbar.message)}
          </Alert>
        </Snackbar>
        <Divider sx={{ my: 2 }} />
        {/* Seedbox update status */}
        {seedboxStatus &&
          (() => {
            /** @type {'success'|'warning'} */
            const seedboxSeverity = seedboxStatus.success ? 'success' : 'warning';
            return (
              <Box sx={{ mb: 2 }}>
                <Alert severity={seedboxSeverity}>
                  {seedboxStatus.msg || seedboxStatus.error || 'Seedbox update status unknown.'}
                </Alert>
              </Box>
            );
          })()}
        {/* Make sure this is the end of CardContent, after all conditional Boxes are closed */}
      </CardContent>
    </Card>
  );
});

export default StatusCard;
