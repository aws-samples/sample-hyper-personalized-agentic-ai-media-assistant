import { useDeviceControls } from './device';
import './DeviceToggle.css';

/**
 * Dev-only floating control to force mobile/TV layout (or clear the override
 * and go back to viewport detection). Rendered only when import.meta.env.DEV.
 */
export function DeviceToggle() {
  const { device, setOverride, overridden } = useDeviceControls();

  return (
    <div className="device-toggle" role="group" aria-label="Preview device">
      <button
        className={device === 'mobile' ? 'active' : ''}
        onClick={() => setOverride('mobile')}
      >
        Mobile
      </button>
      <button
        className={device === 'tv' ? 'active' : ''}
        onClick={() => setOverride('tv')}
      >
        TV
      </button>
      <button
        className={!overridden ? 'active' : ''}
        onClick={() => setOverride(null)}
        title="Auto-detect from viewport"
      >
        Auto
      </button>
    </div>
  );
}
