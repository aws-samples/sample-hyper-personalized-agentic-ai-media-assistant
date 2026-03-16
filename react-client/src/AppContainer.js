import * as React from 'react';
import { useEffect, useState } from "react";


import { ThemeProvider, createTheme } from '@mui/material/styles';
import CssBaseline from '@mui/material/CssBaseline';

import Container from '@mui/material/Container';
import AppBar from '@mui/material/AppBar';
import Toolbar from '@mui/material/Toolbar';
import Button from '@mui/material/Button';
import Typography from '@mui/material/Typography';

import { Amplify } from 'aws-amplify';
import { fetchUserAttributes } from 'aws-amplify/auth';
import { signOut } from 'aws-amplify/auth';
import { useAuthenticator } from '@aws-amplify/ui-react';

import logo from './assets/images/new-logo-color-n-bg.dd7104b589abea6cf209c78b6bbdf374.svg';
import awsLogo from './assets/images/aws-logo-pb.9690272b666f986a10f7.png';
import nabProfile from './assets/images/nab.png';
import architectureImage from './assets/images/architecture.png';

import Box from '@mui/material/Box';
import Divider from '@mui/material/Divider';
import MenuItem from '@mui/material/MenuItem';
import Drawer from '@mui/material/Drawer';
import MenuIcon from '@mui/icons-material/Menu';

import CircularProgress from '@mui/material/CircularProgress';

import Menu from '@mui/material/Menu';
import Avatar from '@mui/material/Avatar';
import Tooltip from '@mui/material/Tooltip';
import IconButton from '@mui/material/IconButton';
import InfoIcon from '@mui/icons-material/Info';
import Dialog from '@mui/material/Dialog';
import DialogContent from '@mui/material/DialogContent';
import DialogTitle from '@mui/material/DialogTitle';
import CloseIcon from '@mui/icons-material/Close';




const logoStyle = {
  width: '200px',
  height: 'auto',
  cursor: 'pointer',
};


const darkTheme = createTheme({
  palette: {
    primary: {
      main: '#FEFFC0',
    },
    mode: 'dark'
  },
});

function AppContainer({ children }) {
  const { user } = useAuthenticator((context) => [context.user]);
  const [loaded, setLoaded] = useState(false);
  const [email, setEmail] = useState("");
  const [username, setUsername] = useState("");


  const [open, setOpen] = React.useState(false);

  const toggleDrawer = (newOpen) => () => {
    setOpen(newOpen);
  };

  const [anchorElUser, setAnchorElUser] = React.useState(null);
  const [architectureModalOpen, setArchitectureModalOpen] = React.useState(false);

  const handleOpenUserMenu = (event) => {
    setAnchorElUser(event.currentTarget);
  };

  const handleCloseUserMenu = () => {
    setAnchorElUser(null);
  };

  const handleOpenArchitectureModal = () => {
    setArchitectureModalOpen(true);
  };

  const handleCloseArchitectureModal = () => {
    setArchitectureModalOpen(false);
  };

  const effectRan = React.useRef(false);

  useEffect(() => {
    if (!effectRan.current) {


      const fetchData = async () => {
        if (loaded == false) {
          try {
            const userAttributes = await fetchUserAttributes();
            setEmail(userAttributes.email);
            setUsername(user?.username || userAttributes.preferred_username || userAttributes.email);
            setLoaded(true);
          } catch (err) {
            console.error('Error fetching user attributes:', err);
          }
        }
      }
      fetchData()
        .catch(console.error);
    }

    return () => effectRan.current = true;
  }, []);

  return (
    <ThemeProvider theme={darkTheme}>
      <CssBaseline />

      <AppBar
        position="static"
        sx={(theme) => ({
          bgcolor: 'rgba(0, 0, 0, 0.2)',
          backdropFilter: 'blur(24px)',
          backgroundImage: 'none',
          flexShrink: 0,
          pt: 1,
          pb: 1,
          boxShadow:
            theme.palette.mode === 'light'
              ? `0 0 1px rgba(85, 166, 246, 0.1), 1px 1.5px 2px -1px rgba(85, 166, 246, 0.11), 4px 4px 22px -2.5px rgba(85, 166, 246, 0.15)`
              : '0 0 1px rgba(0, 0, 0, 0.2), 1px 1.5px 2px -1px rgba(0, 0, 0, 0.2), 4px 4px 32px -2.5px rgba(0, 0, 0, 0.3)'
        })}
      >
        <Container maxWidth="false">
          <Toolbar variant="regular" >
            <Box
              sx={{
                flexGrow: 1,
                display: 'flex',
                alignItems: 'center',
                ml: '-20px',
                px: 0
              }}
            >
              <img
                src={logo}
                alt="Builder Flix"
                style={logoStyle}
              />

            </Box>
            <Box
              sx={{
                display: { xs: 'none', md: 'flex' },
                gap: 0.5,
                alignItems: 'center',
              }}
            >
              <Box sx={{ 
                display: 'flex', 
                alignItems: 'center', 
                gap: 1.5
              }}>
                <Typography variant="body1" sx={{ fontWeight: 500 }}>
                  {username || 'User'}
                </Typography>
                <Tooltip title="Open settings">
                  <IconButton onClick={handleOpenUserMenu} sx={{ p: 0 }}>
                    <Avatar 
                      alt={username || 'User'} 
                      src={nabProfile}
                      sx={{ width: 40, height: 40 }}
                    />
                  </IconButton>
                </Tooltip>
                <Menu
                  sx={{ mt: '45px' }}
                  id="menu-appbar"
                  anchorEl={anchorElUser}
                  anchorOrigin={{
                    vertical: 'top',
                    horizontal: 'right',
                  }}
                  keepMounted
                  transformOrigin={{
                    vertical: 'top',
                    horizontal: 'right',
                  }}
                  open={Boolean(anchorElUser)}
                  onClose={handleCloseUserMenu}
                >
                  <MenuItem onClick={signOut}>
                    <Typography textAlign="center">Sign out</Typography>
                  </MenuItem>
                </Menu>
              </Box>
            </Box>
            <Box sx={{ display: { sm: '', md: 'none' } }}>
              <Button
                variant="text"
                color="primary"
                aria-label="menu"
                onClick={toggleDrawer(true)}
                sx={{ minWidth: '30px', p: '4px' }}
              >
                <MenuIcon />
              </Button>
              <Drawer anchor="right" open={open} onClose={toggleDrawer(false)}>
                <Box
                  sx={{
                    minWidth: '60dvw',
                    p: 2,
                    backgroundColor: 'background.paper',
                    flexGrow: 1,
                  }}
                >

                  <Divider />
                  <MenuItem>
                    <Button
                      color="primary"
                      variant="outlined"
                      component="a"
                      href="/"
                      onClick={signOut}
                      sx={{ width: '100%' }}
                    >
                      Sign out
                    </Button>
                  </MenuItem>
                </Box>
              </Drawer>
            </Box>
          </Toolbar>
        </Container>
      </AppBar>

      {(loaded === true) ? (
        <Container maxWidth="false" sx={{ py: 3 }}>
          {children}
        </Container>
      ) : (
        <Box alignItems="center"
          justifyContent="center" sx={{ display: 'flex', p: 5 }}>
          <CircularProgress />
        </Box>
      )}

      <Box
        sx={{
          position: 'fixed',
          bottom: '10px',
          right: '32px',
          zIndex: 'tooltip',
          display: 'flex',
          alignItems: 'center',
          gap: 1
        }}
      >
        <img src={awsLogo} alt="Powered By AWS" style={{ height: '40px', width: 'auto' }} />
        <IconButton
          onClick={handleOpenArchitectureModal}
          sx={{
            backgroundColor: 'rgba(255, 255, 255, 0.1)',
            '&:hover': {
              backgroundColor: 'rgba(255, 255, 255, 0.2)',
            },
            width: '48px',
            height: '48px'
          }}
          size="medium"
        >
          <InfoIcon sx={{ fontSize: '28px', color: 'white' }} />
        </IconButton>
      </Box>

      <Dialog
        open={architectureModalOpen}
        onClose={handleCloseArchitectureModal}
        maxWidth="xl"
        fullWidth
        PaperProps={{
          sx: {
            minHeight: '80vh',
            maxHeight: '90vh'
          }
        }}
      >
        <DialogTitle sx={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          fontSize: '1.5rem',
          fontWeight: 'bold'
        }}>
          Architecture Overview
          <IconButton onClick={handleCloseArchitectureModal} size="large">
            <CloseIcon sx={{ fontSize: '32px' }} />
          </IconButton>
        </DialogTitle>
        <DialogContent sx={{ p: 4 }}>
          <Box sx={{
            textAlign: 'center',
            display: 'flex',
            justifyContent: 'center',
            alignItems: 'center',
            minHeight: '60vh'
          }}>
            <img
              src={architectureImage}
              alt="System Architecture"
              style={{
                width: '95%',
                maxWidth: '1200px',
                height: 'auto',
                borderRadius: '12px',
                boxShadow: '0 8px 24px rgba(0,0,0,0.15)',
                border: '1px solid rgba(255,255,255,0.1)'
              }}
            />
          </Box>
        </DialogContent>
      </Dialog>

    </ThemeProvider>
  );
}

export default AppContainer;