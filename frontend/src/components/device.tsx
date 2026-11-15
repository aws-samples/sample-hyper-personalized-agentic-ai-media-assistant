import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';
import type { DeviceMode } from '../data/types';
import { setEventDevice } from '../lib/events';

/**
 * The mockups render mobile and TV layouts side by side inside a gallery.
 * The real app is a single responsive codebase: the device mode is derived
 * from the viewport (phone browser -> mobile, big screen -> TV), with an
 * optional override so we can force a mode (e.g. on a kiosk screen) or preview both
 * layouts on one machine during development.
 *
 * Override precedence: ?device=mobile|tv  >  localStorage("hpm-device")  >  viewport.
 */

const TV_MIN_WIDTH = 900; // px; below this we treat the viewport as mobile
const OVERRIDE_KEY = 'hpm-device';

interface DeviceContextValue {
  device: DeviceMode;
  /** Force a device mode (persists), or pass null to clear and auto-detect. */
  setOverride: (mode: DeviceMode | null) => void;
  /** Whether the current mode comes from an explicit override. */
  overridden: boolean;
}

const DeviceContext = createContext<DeviceContextValue>({
  device: 'mobile',
  setOverride: () => {},
  overridden: false,
});

function readOverride(): DeviceMode | null {
  const fromUrl = new URLSearchParams(window.location.search).get('device');
  if (fromUrl === 'mobile' || fromUrl === 'tv') return fromUrl;
  const stored = localStorage.getItem(OVERRIDE_KEY);
  return stored === 'mobile' || stored === 'tv' ? stored : null;
}

function detectFromViewport(): DeviceMode {
  return window.innerWidth >= TV_MIN_WIDTH ? 'tv' : 'mobile';
}

export function DeviceProvider({ children }: { children: ReactNode }) {
  const [override, setOverrideState] = useState<DeviceMode | null>(() =>
    readOverride(),
  );
  const [viewportMode, setViewportMode] = useState<DeviceMode>(() =>
    detectFromViewport(),
  );

  useEffect(() => {
    const onResize = () => setViewportMode(detectFromViewport());
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);

  const setOverride = (mode: DeviceMode | null) => {
    if (mode) {
      localStorage.setItem(OVERRIDE_KEY, mode);
    } else {
      localStorage.removeItem(OVERRIDE_KEY);
    }
    setOverrideState(mode);
  };

  const device = override ?? viewportMode;

  useEffect(() => {
    setEventDevice(device);
  }, [device]);

  const value = useMemo<DeviceContextValue>(
    () => ({ device, setOverride, overridden: override !== null }),
    [device, override],
  );

  return <DeviceContext.Provider value={value}>{children}</DeviceContext.Provider>;
}

export function useDevice(): DeviceMode {
  return useContext(DeviceContext).device;
}

export function useDeviceControls(): DeviceContextValue {
  return useContext(DeviceContext);
}

export const isTV = (m: DeviceMode) => m === 'tv';
export const isMobile = (m: DeviceMode) => m === 'mobile';
