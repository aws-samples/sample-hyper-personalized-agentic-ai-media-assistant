import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import './styles/global.css';
import App from './App.tsx';
import { SessionProvider } from './lib/session';
import { HouseholdProvider } from './lib/household';
import { DeviceProvider } from './components/device';
import { initTheme } from './lib/theme';

initTheme();

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <SessionProvider>
      <HouseholdProvider>
        <DeviceProvider>
          <App />
        </DeviceProvider>
      </HouseholdProvider>
    </SessionProvider>
  </StrictMode>,
);
