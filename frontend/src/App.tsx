import {
  BrowserRouter,
  Routes,
  Route,
  Navigate,
  useLocation,
  type Location,
} from 'react-router-dom';
import type { ReactNode } from 'react';
import { useDevice } from './components/device';
import { DeviceToggle } from './components/DeviceToggle';
import { useSession } from './lib/session';
import { useHousehold } from './lib/household';
import { Login } from './screens/Login/Login';
import { ProfileSelection } from './screens/ProfileSelection/ProfileSelection';
import { Preferences } from './screens/Preferences/Preferences';
import { Home } from './screens/Home/Home';
import { Search } from './screens/Search/Search';
import { Clips } from './screens/Clips/Clips';
import {
  TitleRoute,
  WatchRoute,
  LiveRoute,
  LiveWatchRoute,
} from './screens/ContentRoutes';

/** Redirect to /login unless a valid Cognito session exists. */
function RequireAuth({ children }: { children: ReactNode }) {
  const { authed, loading } = useSession();
  if (loading) return <div className="screen" />; // brief: restoring session
  if (!authed) return <Navigate to="/login" replace />;
  return <>{children}</>;
}

/**
 * Redirect to /profiles until a profile has been chosen. A stored choice that
 * isn't one of this account's profiles (e.g. an old persona id, or another
 * user's member) also goes back to /profiles once the household has loaded.
 */
function RequireProfile({ children }: { children: ReactNode }) {
  const { profileId } = useSession();
  const { state, byId } = useHousehold();
  if (!profileId) return <Navigate to="/profiles" replace />;
  if (state === 'idle' || state === 'loading') return <div className="screen" />;
  if (!byId(profileId)) return <Navigate to="/profiles" replace />;
  return <>{children}</>;
}

/** Wrap a protected element in auth + profile guards. */
const guard = (el: ReactNode) => (
  <RequireAuth>
    <RequireProfile>{el}</RequireProfile>
  </RequireAuth>
);

/**
 * Content overlays (title detail, VOD player, live game entry/player) are
 * real routes so they're deep-linkable, but they render *over* the screen you
 * launched from. We use react-router's background-location pattern: when a
 * launcher navigates with `state.background`, the main <Routes> keeps rendering
 * that background screen while a second <Routes> renders the overlay on top.
 * A direct visit (no background) renders the overlay standalone.
 */
function AppRoutes() {
  const location = useLocation();
  const state = location.state as { background?: Location } | null;
  const background = state?.background;

  const overlayRoutes = (
    <Routes>
      <Route path="/title/:id" element={guard(<TitleRoute />)} />
      <Route path="/watch/:id" element={guard(<WatchRoute />)} />
      <Route path="/live/:id" element={guard(<LiveRoute />)} />
      <Route path="/watch/live/:id" element={guard(<LiveWatchRoute />)} />
    </Routes>
  );

  return (
    <>
      <Routes location={background || location}>
        <Route path="/" element={<Navigate to="/login" replace />} />
        <Route path="/login" element={<Login />} />
        <Route path="/profiles" element={<RequireAuth><ProfileSelection /></RequireAuth>} />
        <Route path="/preferences/:profileId" element={<RequireAuth><Preferences /></RequireAuth>} />
        <Route path="/home" element={guard(<Home />)} />
        <Route path="/search" element={guard(<Search />)} />
        <Route path="/clips" element={guard(<Clips />)} />
        {/* Deep-linkable to a specific clip, like the IVS feed demo's /:id. */}
        <Route path="/clips/:clipId" element={guard(<Clips />)} />
        {/* Also reachable directly (no background) for deep links / refresh. */}
        <Route path="/title/:id" element={guard(<TitleRoute />)} />
        <Route path="/watch/:id" element={guard(<WatchRoute />)} />
        <Route path="/live/:id" element={guard(<LiveRoute />)} />
        <Route path="/watch/live/:id" element={guard(<LiveWatchRoute />)} />
        <Route path="*" element={<Navigate to="/login" replace />} />
      </Routes>
      {background && overlayRoutes}
    </>
  );
}

export default function App() {
  const device = useDevice();

  return (
    <div className={`app-root device-${device}`}>
      <BrowserRouter>
        <AppRoutes />
      </BrowserRouter>
      {import.meta.env.DEV && <DeviceToggle />}
    </div>
  );
}
