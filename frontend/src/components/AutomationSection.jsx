import React from 'react';
import {
  Box,
  Typography,
  Tooltip,
  FormControlLabel,
  Checkbox,
  FormControl,
  InputLabel,
  Select,
  MenuItem,
  TextField,
} from '@mui/material';

/**
 * Generic automation section for PerkAutomationCard (Wedge, VIP, Upload, etc)
 * Props:
 * - title: string
 * - enabled: boolean
 * - onToggle: function
 * - toggleLabel: string
 * - toggleDisabled: boolean
 * - selectLabel: string
 * - selectValue: any
 * - selectOptions: array of { value, label }
 * - onSelectChange: function
 * - extraControls: React node (optional)
 * - triggerType: string
 * - onTriggerTypeChange: function
 * - triggerTypeValue: string
 * - triggerDays: number
 * - onTriggerDaysChange: function
 * - triggerPointThreshold: number
 * - onTriggerPointThresholdChange: function
 * - confirmButton: React node (optional)
 * - tooltip: string (optional)
 */
export default function AutomationSection({
  title,
  enabled,
  onToggle,
  toggleLabel,
  toggleDisabled = false,
  selectLabel,
  selectValue,
  selectOptions = [],
  onSelectChange,
  extraControls = null,
  onTriggerTypeChange,
  triggerTypeValue,
  triggerDays,
  onTriggerDaysChange,
  triggerPointThreshold,
  onTriggerPointThresholdChange,
  confirmButton = null,
  tooltip = '',
}) {
  return (
    <Box sx={{ mb: 3 }}>
      <Typography variant="subtitle1" sx={{ mb: 1 }}>
        {title}
      </Typography>
      <Box
        sx={{
          display: 'flex',
          alignItems: 'center',
          width: '100%',
          mb: 2,
        }}
      >
        <Tooltip title={tooltip} disableHoverListener={!tooltip} arrow>
          <span>
            <FormControlLabel
              control={<Checkbox checked={enabled} onChange={onToggle} disabled={toggleDisabled} />}
              label={<span>{toggleLabel}</span>}
              sx={{
                minWidth: 220,
                mr: 3,
                whiteSpace: 'nowrap',
                flexShrink: 0,
              }}
            />
          </span>
        </Tooltip>
        {selectLabel && (
          <FormControl size="small" sx={{ minWidth: 120, mr: 1, flexShrink: 0 }}>
            <InputLabel>{selectLabel}</InputLabel>
            <Select
              value={selectValue}
              label={selectLabel}
              onChange={onSelectChange}
              MenuProps={{ disableScrollLock: true }}
            >
              {selectOptions.map((opt) => (
                <MenuItem key={opt.value} value={opt.value}>
                  {opt.label}
                </MenuItem>
              ))}
            </Select>
          </FormControl>
        )}
        {extraControls}
        <Box sx={{ flexGrow: 1 }} />
        {confirmButton}
      </Box>
      {/* Trigger options row */}
      <Box sx={{ display: 'flex', gap: 2, alignItems: 'center', mt: 1, mb: 2 }}>
        <FormControl size="small" sx={{ minWidth: 160 }}>
          <InputLabel>Trigger Type</InputLabel>
          <Select
            value={triggerTypeValue}
            label="Trigger Type"
            onChange={onTriggerTypeChange}
            MenuProps={{ disableScrollLock: true }}
          >
            <MenuItem value="time">Time-based</MenuItem>
            <MenuItem value="points">Point-based</MenuItem>
            <MenuItem value="both">Both</MenuItem>
          </Select>
        </FormControl>
        {(triggerTypeValue === 'time' || triggerTypeValue === 'both') && (
          <TextField
            label="Every X Days"
            type="number"
            value={triggerDays}
            onChange={onTriggerDaysChange}
            size="small"
            sx={{ minWidth: 120 }}
          />
        )}
        {(triggerTypeValue === 'points' || triggerTypeValue === 'both') && (
          <TextField
            label="Point Threshold"
            type="number"
            value={triggerPointThreshold}
            onChange={onTriggerPointThresholdChange}
            size="small"
            sx={{ minWidth: 140 }}
          />
        )}
      </Box>
    </Box>
  );
}
