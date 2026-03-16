import React from 'react';
import { Authenticator, ThemeProvider } from '@aws-amplify/ui-react';
import './custom-auth-theme.css';
import logo from './assets/images/new-logo-color-n-bg.dd7104b589abea6cf209c78b6bbdf374.svg';
import poweredByAws from './assets/images/Powered-By_logo-horiz_RGB_REV.png';

const theme = {
  name: 'dark-theme',
  tokens: {
    colors: {
      background: {
        primary: '#1a1a1a',
        secondary: '#2d2d2d'
      },
      font: {
        primary: '#ffffff'
      }
    }
  }
};

const CustomAuthenticator = ({ children }) => {
  const components = {
    Header() {
      return (
        <div style={{ textAlign: 'center', padding: 'var(--amplify-space-large)' }}>
          <img className="amplify-image" alt="Builder Flix" src={logo} style={{ margin: '48px 0px 0px' }} />
        </div>
      );
    },
    Footer() {
      return (
        <div style={{ padding: '0px', margin: '0px', textAlign: 'center' }}>
          <img src={poweredByAws} alt="Powered By AWS" />
        </div>
      );
    }
  };

  return (
    <ThemeProvider theme={theme} colorMode="dark">
      <Authenticator 
        components={components}
        hideSignUp={true}
      >
        {children}
      </Authenticator>
    </ThemeProvider>
  );
};

export default CustomAuthenticator;