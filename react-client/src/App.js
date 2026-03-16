import { useAuthenticator } from "@aws-amplify/ui-react";
import "@aws-amplify/ui-react/styles.css";
import S2sChatBot from './s2s'
import CustomAuthenticator from './CustomAuthenticator';
import AppContainer from './AppContainer';
import { Amplify } from 'aws-amplify';

const AppContent = () => {
  const { user } = useAuthenticator((context) => [context.user]);

  return (
    <AppContainer>
      <S2sChatBot user={user} />
    </AppContainer>
  );
};

const App = () => {
  return (
    <CustomAuthenticator>
      <AppContent />
    </CustomAuthenticator>
  );
};

export default App;