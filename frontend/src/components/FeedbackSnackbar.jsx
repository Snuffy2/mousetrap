import { Snackbar, Alert } from '@mui/material';
import { stringifyMessage } from '../utils/utils';

/**
 * Reusable snackbar for showing feedback messages.
 *
 * @param {Object} props
 * @param {boolean} props.open
 * @param {any} props.message
 * @param {'success'|'error'|'info'|'warning'} [props.severity]
 * @param {(event?: Event | import('react').SyntheticEvent, reason?: import('@mui/material').SnackbarCloseReason) => void} props.onClose
 */
export default function FeedbackSnackbar({ open, message, severity = 'info', onClose }) {
  return (
    <Snackbar open={open} autoHideDuration={6000} onClose={onClose}>
      <Alert onClose={onClose} severity={severity} sx={{ width: '100%' }}>
        {stringifyMessage(message)}
      </Alert>
    </Snackbar>
  );
}
