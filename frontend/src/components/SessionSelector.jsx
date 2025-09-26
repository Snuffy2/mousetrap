import React, { useEffect, useState } from 'react';
import {
  FormControl,
  InputLabel,
  Select,
  MenuItem,
  IconButton,
  Box,
  Tooltip,
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  Button,
  Typography,
} from '@mui/material';
import AddCircleOutlineIcon from '@mui/icons-material/AddCircleOutline';
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline';

import { useSession } from '../context/SessionContext';

export default function SessionSelector({ onLoadSession, onCreateSession, onDeleteSession, sx }) {
  const { sessionLabel: selectedLabel, setSessionLabel: setSelectedLabel } = useSession();
  const [sessions, setSessions] = useState([]);
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);

  useEffect(() => {
    fetch('/api/sessions')
      .then((res) => res.json())
      .then((data) => setSessions(data.sessions || []));
  }, [selectedLabel]);

  const handleChange = (e) => {
    setSelectedLabel(e.target.value);
    if (onLoadSession) onLoadSession(e.target.value);
    // Only persist to backend if the label exists in the sessions list
    if (sessions.includes(e.target.value)) {
      fetch('/api/last_session', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ label: e.target.value }),
      });
    }
  };

  const handleDeleteClick = () => {
    setDeleteDialogOpen(true);
  };

  const handleDeleteConfirm = () => {
    setDeleteDialogOpen(false);
    onDeleteSession(selectedLabel);
  };

  const handleDeleteCancel = () => {
    setDeleteDialogOpen(false);
  };

  return (
    <Box sx={{ display: 'flex', alignItems: 'center', ...sx }}>
      <FormControl size="small" sx={{ minWidth: 160, maxWidth: 240 }}>
        <InputLabel>Session</InputLabel>
        <Select
          value={selectedLabel}
          label="Session"
          onChange={handleChange}
          sx={{ width: 180 }}
          MenuProps={{ disableScrollLock: true }}
        >
          {sessions.map((label) => (
            <MenuItem key={label} value={label}>
              {label}
            </MenuItem>
          ))}
        </Select>
      </FormControl>
      <Tooltip title="Create New Session">
        <IconButton color="success" sx={{ ml: 1 }} onClick={onCreateSession}>
          <AddCircleOutlineIcon />
        </IconButton>
      </Tooltip>
      <Tooltip title="Delete Session">
        <span>
          <IconButton
            color="error"
            sx={{ ml: 1 }}
            onClick={handleDeleteClick}
            disabled={sessions.length === 0}
          >
            <DeleteOutlineIcon />
          </IconButton>
        </span>
      </Tooltip>
      <Dialog open={deleteDialogOpen} onClose={handleDeleteCancel} disableScrollLock={true}>
        <DialogTitle>Delete Session</DialogTitle>
        <DialogContent>
          <Typography>
            Are you sure you want to delete session <b>{selectedLabel}</b>? This cannot be undone.
          </Typography>
        </DialogContent>
        <DialogActions>
          <Button onClick={handleDeleteCancel} color="primary" variant="outlined">
            Cancel
          </Button>
          <Button onClick={handleDeleteConfirm} color="error" variant="contained">
            Delete
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
