import type { Device, Reading } from '../api/types';

export function timeAgo(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return 'never';
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return 'unknown';
  const seconds = Math.max(0, Math.round((now.getTime() - then) / 1000));
  if (seconds < 60) return 'just now';
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours} h ago`;
  return `${Math.round(hours / 24)} days ago`;
}

export function readingLabel(reading: Reading): string {
  switch (reading.kind) {
    case 'heart_rate': return `Heart rate ${Math.round(reading.value)} bpm`;
    case 'steps': return `${Math.round(reading.value)} steps`;
    case 'movement': return `Movement ${Math.round(reading.value)}`;
    case 'on_wrist': return reading.value >= 0.5 ? 'Watch on the wrist' : 'Watch off the wrist';
    case 'battery': return `Battery ${Math.round(reading.value)} %`;
    default: return `${reading.kind} ${reading.value}`;
  }
}

export function deviceSummary(device: Device | undefined, now: Date = new Date()): string {
  if (!device) return 'No watch paired';
  const parts: string[] = [];
  if (device.on_wrist === false) parts.push('off the wrist');
  if (device.battery_percent !== null && device.battery_percent !== undefined) parts.push(`battery ${device.battery_percent} %`);
  if (device.background_sensor_permission === false) parts.push('sensor permission off');
  parts.push(`synced ${timeAgo(device.last_sync_at, now)}`);
  return parts.join(', ');
}

/** A telephone link the phone can open; empty when there is no number. */
export function telLink(phone: string): string {
  const digits = phone.replace(/[^\d+]/g, '');
  return digits ? `tel:${digits}` : '';
}
