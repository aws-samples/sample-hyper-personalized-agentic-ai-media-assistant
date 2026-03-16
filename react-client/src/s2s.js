import React, { createRef } from 'react';
import ReactPlayer from 'react-player'

import './s2s.css'
import { Box, Typography, Dialog, DialogTitle, DialogContent, DialogActions, Button, Alert, TextField, Select, MenuItem, FormControl, InputLabel } from '@mui/material';
import Grid from '@mui/material/Unstable_Grid2';
import { Icon } from '@cloudscape-design/components';
import S2sEvent from './helper/s2sEvents';
import { base64LPCM } from './helper/audioHelper';
import StarRating from './stars';

import sonicGif from './assets/images/nova_sonic.gif';

class S2sChatBot extends React.Component {

    constructor(props) {
        super(props);
        this.state = {
            alert: null,
            sessionStarted: false,
            showEventJson: false,
            showConfig: false,
            selectedEvent: null,
            mute: false,
            chatMessages: {},
            audioResponse: {},
            audioChunks: [],
            promptName: null,
            textContentName: null,
            audioContentName: null,
            websocketConnected: false,
            voiceAssistantCollapsed: true,
            playerVideoURL: "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4",
            isVideoPlaying: false,

            isAssistantThinking: false,

            // S2S config items
            configSystemPrompt: S2sEvent.DEFAULT_SYSTEM_PROMPT,
            configVoiceIdOption: { label: "Matthew (en-US)", value: "matthew" },
            configToolUse: JSON.stringify(S2sEvent.DEFAULT_TOOL_CONFIG, null, 2),

            // New text input state
            textInput: '',
            inputMode: 'voice' // 'voice' or 'text'
        };
        this.socket = null;
        this.mediaRecorder = null;
        this.chatMessagesEndRef = React.createRef();
        this.chatAreaRef = React.createRef();
        this.audioPlayerRef = createRef();
        this.videoPlayerRef = createRef();
        this.audioQueue = [];
        this.isPlaying = false;

        this.closeWebSocket = this.closeWebSocket.bind(this);
        this.toggleVoiceAssistant = this.toggleVoiceAssistant.bind(this);
        this.handleTextInputChange = this.handleTextInputChange.bind(this);
        this.handleTextSubmit = this.handleTextSubmit.bind(this);
        this.handleKeyPress = this.handleKeyPress.bind(this);
    }

    scrollChatToBottom() {
        // Use requestAnimationFrame for smooth, immediate scrolling
        requestAnimationFrame(() => {
            if (this.chatAreaRef.current) {
                this.chatAreaRef.current.scrollTop = this.chatAreaRef.current.scrollHeight;
            }
        });
    }

    componentDidUpdate(prevProps, prevState) {
        if (Object.keys(prevState.chatMessages).length !== Object.keys(this.state.chatMessages).length) {
            this.scrollChatToBottom();
        }
        if (prevState.playerVideoURL !== this.state.playerVideoURL) {
            // Scroll to show new video content
            this.chatMessagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
        }
    }

    sendEvent(event) {
        if (this.socket && this.socket.readyState === WebSocket.OPEN) {
            this.socket.send(JSON.stringify(event));
            event.timestamp = Date.now();
        }
    }

    cancelAudio() {
        this.audioQueue = [];
        this.isPlaying = false;
        if (this.audioPlayerRef.current) {
            this.audioPlayerRef.current.pause();
            this.audioPlayerRef.current.src = '';
        }
    }

    enqueueAudio(audioUrl) {
        this.audioQueue.push(audioUrl);
        if (!this.isPlaying) {
            this.playNextAudio();
        }
    }

    playNextAudio() {
        if (this.isPlaying || this.audioQueue.length === 0 || !this.audioPlayerRef.current) return;

        let audioUrl = this.audioQueue.shift();
        this.isPlaying = true;

        // Optimize for faster audio playback - avoid load() call when possible
        const audioElement = this.audioPlayerRef.current;
        audioElement.src = audioUrl;
        
        // Use preload="auto" and play immediately for lower latency
        audioElement.preload = "auto";
        audioElement.play().catch(error => {
            console.error('Audio playback failed:', error);
            this.isPlaying = false;
            this.playNextAudio(); // Try next audio in queue
        });

        audioElement.onended = () => {
            this.isPlaying = false;
            this.playNextAudio();
        };
    }

    handleIncomingMessage(message) {
        // Handle error messages from backend
        if (message && message.type === 'error') {
            console.error('Backend error:', message.message);
            this.setState({ 
                alert: message.message,
                websocketConnected: false,
                sessionStarted: false 
            });
            return;
        }

        // Skip if no event (but don't warn - might be normal)
        if (!message?.event) {
            return;
        }

        const eventType = Object.keys(message.event)[0];
        const role = message.event[eventType]["role"];
        const content = message.event[eventType]["content"];
        const contentId = message.event[eventType].contentId;
        let stopReason = message.event[eventType].stopReason;
        const contentType = message.event[eventType].type;

        switch (eventType) {
            case "poster":
                // Show posters immediately without waiting
                this.setState(prevState => ({
                    chatMessages: {
                        ...prevState.chatMessages,
                        [contentId]: {
                            ...message.event,
                            raw: [{ timestamp: Date.now(), event: message.event }]
                        }
                    }
                }));
                break;

            case "textOutput":
                // Detect interruption
                if (role === "ASSISTANT" && content.startsWith("{")) {
                    const evt = JSON.parse(content);
                    if (evt.interrupted === true) {
                        this.cancelAudio();
                    }
                }

                this.setState(prevState => {
                    const chatMessages = { ...prevState.chatMessages };
                    if (chatMessages.hasOwnProperty(contentId)) {
                        chatMessages[contentId] = {
                            ...chatMessages[contentId],
                            content: content,
                            role: role,
                            raw: [...(chatMessages[contentId].raw || []), message]
                        };
                    }
                    return { chatMessages };
                }, () => {
                    this.scrollChatToBottom();
                });
                break;
            case "audioOutput":
                this.setState(prevState => ({
                    audioResponse: {
                        ...prevState.audioResponse,
                        [contentId]: (prevState.audioResponse[contentId] || "") + message.event[eventType].content
                    }
                }));
                break;
            case "contentStart":
                if (contentType === "AUDIO") {
                    this.setState(prevState => ({
                        audioResponse: {
                            ...prevState.audioResponse,
                            [contentId]: ""
                        }
                    }));
                }
                else if (contentType === "TEXT") {
                    var generationStage = "";
                    if (message.event.contentStart.additionalModelFields) {
                        generationStage = JSON.parse(message.event.contentStart.additionalModelFields)?.generationStage;
                    }
                    this.setState(prevState => ({
                        chatMessages: {
                            ...prevState.chatMessages,
                            [contentId]: {
                                content: "",
                                role: role,
                                generationStage: generationStage,
                                raw: [message]
                            }
                        }
                    }), () => {
                        this.scrollChatToBottom();
                    });
                }
                break;
            case "contentEnd":
                if (contentType === "AUDIO") {
                    // Process audio asynchronously to avoid blocking the main thread
                    requestAnimationFrame(() => {
                        const audioUrl = base64LPCM(this.state.audioResponse[contentId]);
                        this.enqueueAudio(audioUrl);
                    });
                }
                else if (contentType === "TEXT") {
                    this.setState(prevState => {
                        const chatMessages = { ...prevState.chatMessages };
                        if (chatMessages.hasOwnProperty(contentId)) {
                            chatMessages[contentId] = {
                                ...chatMessages[contentId],
                                raw: [...(chatMessages[contentId].raw || []), message],
                                stopReason: stopReason
                            };
                        }

                        // If user's turn just ended, show thinking animation
                        const isAssistantThinking = chatMessages[contentId]?.role === "USER";
                        
                        return {
                            chatMessages,
                            isAssistantThinking
                        };
                    }, () => {
                        // Only scroll if assistant is responding (not thinking)
                        if (!this.state.isAssistantThinking) {
                            this.scrollChatToBottom();
                        }
                    });
                }
                break;
            case "completionStart":
                // Completion started - assistant is beginning to respond
                console.log('[S2S] Completion started');
                break;
            case "toolUse":
                // Tool is being called - log for debugging
                console.log('[S2S] Tool use event:', message.event.toolUse);
                // Turn off thinking animation since tool is being processed
                this.setState({ isAssistantThinking: false });
                break;
            case "authorization":
                // Authorization event
                console.log('[S2S] Authorization event');
                break;
            default:
                // Handle unknown event types
                console.warn('[S2S] Unknown event type:', eventType, message);
                break;
        }
    }

    handleAudioMuteRequest = e => {
        this.setState({ mute: !this.state.mute });
        const audioTrack = this.stream.getAudioTracks()[0];
        if (this.state.mute) {
            // Mute the microphone
            audioTrack.enabled = true;
        }
        else {
            // Unmute the microphone
            audioTrack.enabled = false;
        }
    }

    handleSessionChange = e => {
        if (this.state.sessionStarted) {
            // End session and disconnect WebSocket
            this.endSession();
            this.closeWebSocket();
            this.setState({ sessionStarted: false });
        }
        else {
            // Clear previous data and connect WebSocket
            this.setState({
                chatMessages: {},
                audioResponse: {}
            });
            this.cancelAudio();
            this.connectWebSocket();
            // sessionStarted will be set to true in the WebSocket onopen handler
        }
    }

    closeWebSocket() {
        if (this.socket) {
            // Close socket cleanly
            this.socket.close(1000, 'User requested disconnect');
            this.socket = null;
            this.setState({
                websocketConnected: false,
                alert: null // Clear any existing alerts
            });
            // Clear audio processing state
            this.cancelAudio();
        }
    }

    toggleVoiceAssistant() {
        this.setState({ voiceAssistantCollapsed: !this.state.voiceAssistantCollapsed });
    }

    getVideoNameFromURL(url) {
        // Only return specific filename for videos with scene analysis data
        if (url.includes('tears-of-steel')) {
            return "tears-of-steel.mp4";
        }
        
        // Return null for unknown videos (no scene analysis available)
        return null;
    }

    getCurrentMovieInfo() {
        // Handle S3 URL (actual Tears of Steel video with backend scene data)
        if (this.state.playerVideoURL.includes('tears-of-steel')) {
            return {
                title: "Tears of Steel",
                description: "Thom just wanted to be an astronaut. His girlfriend Celia just wanted to create robots - and for him to not be freaked out by her cyborg hand. How was Thom supposed to know that breaking up with her would make her take out her anger on the rest of humanity using her robots? It seems the only possible way of undoing everything...is to overwrite her memory of what happened 40 years ago."
            };
        }

        // Default fallback for Big Buck Bunny and any other videos
        return {
            title: "Now Playing",
            description: "Enjoy this movie selection. Ask the voice assistant about what's happening in this scene or for more recommendations!"
        };
    }

    async connectWebSocket() {
        // Connect to the S2S WebSocket server
        if (this.socket === null || this.socket.readyState !== WebSocket.OPEN) {
            // Get fresh Cognito token
            let token;
            try {
                const { fetchAuthSession } = await import('aws-amplify/auth');
                const session = await fetchAuthSession();
                token = session.tokens?.accessToken?.toString();
                
                if (!token) {
                    this.setState({ alert: "Authentication required" });
                    return;
                }
            } catch (error) {
                console.error('Authentication error:', error);
                this.setState({ alert: "Authentication failed" });
                return;
            }

            this.socket = new WebSocket(process.env.REACT_APP_WEBSOCKET_URL + "/interact-s2s");
            this.socket.addEventListener('open', () => {
                // Send authorization header after connection
                const authMsg = {
                    type: 'authorization',
                    token: `Bearer ${token}`,
                    username: this.props.user.username
                };
                this.socket.send(JSON.stringify(authMsg));
            });

            this.socket.onopen = () => {
                console.log("WebSocket connected!");
                this.setState({
                    websocketConnected: true,
                    alert: null // Clear any connection alerts
                });
                const promptName = crypto.randomUUID();
                const textContentName = crypto.randomUUID();
                const audioContentName = crypto.randomUUID();
                this.setState({
                    promptName: promptName,
                    textContentName: textContentName,
                    audioContentName: audioContentName,
                    isVideoPlaying: false
                });

                // Start session events
                this.sendEvent(S2sEvent.sessionStart());

                var audioConfig = S2sEvent.DEFAULT_AUDIO_OUTPUT_CONFIG;
                audioConfig.voiceId = this.state.configVoiceIdOption.value;
                var toolConfig = this.state.configToolUse ? JSON.parse(this.state.configToolUse) : S2sEvent.DEFAULT_TOOL_CONFIG;
                const promptStart = S2sEvent.promptStart(promptName, audioConfig, toolConfig);
                this.sendEvent(S2sEvent.promptStart(promptName, audioConfig, toolConfig));

                this.sendEvent(S2sEvent.contentStartText(promptName, textContentName));

                this.sendEvent(S2sEvent.textInput(promptName, textContentName, audioConfig.voiceId, this.state.configSystemPrompt));
                this.sendEvent(S2sEvent.contentEnd(promptName, textContentName));
                this.sendEvent(S2sEvent.contentStartAudio(promptName, audioContentName));

                // Get current video playback position in milliseconds
                let currentTimeSeconds = 0;

                // Try multiple approaches to get video time
                let videoElement = document.querySelector('video');

                // If no standard video element, try HLS video element
                if (!videoElement) {
                    const hlsElement = document.querySelector('hls-video');
                    if (hlsElement && hlsElement.currentTime !== undefined) {
                        currentTimeSeconds = hlsElement.currentTime;
                    }
                } else {
                    currentTimeSeconds = videoElement.currentTime;
                }

                const timecodemillis = Math.floor(currentTimeSeconds * 1000);
                const video_name = this.getVideoNameFromURL(this.state.playerVideoURL);

                // Only send watch log event if it's the video with backend scene analysis data
                if (video_name === "tears-of-steel.mp4") {
                    const user_watch_log_event = {
                        "type": 'user_watch_log',
                        "timemillis": timecodemillis,
                        "username": this.props.user.username,
                        "video_name": video_name
                    };
                    console.log("Sending watch log:", user_watch_log_event);
                    this.socket.send(JSON.stringify(user_watch_log_event));
                } else {
                    console.log("Skipping watch log for video without scene analysis:", this.state.playerVideoURL);
                }

                // Always start the session with microphone (required for Nova Sonic 2.0)
                this.startSession();
                this.setState({ sessionStarted: true });
                
                // Send pending text message if exists
                if (this.pendingTextMessage) {
                    const text = this.pendingTextMessage;
                    this.pendingTextMessage = null;
                    
                    setTimeout(() => {
                        const contentId = crypto.randomUUID();
                        this.setState(prevState => ({
                            chatMessages: {
                                ...prevState.chatMessages,
                                [contentId]: {
                                    content: text,
                                    role: "USER",
                                    raw: [{ timestamp: Date.now(), event: { textOutput: { content: text, role: "USER", contentId } } }]
                                }
                            },
                            isAssistantThinking: true
                        }), () => {
                            this.scrollChatToBottom();
                        });

                        // Use Nova Sonic 2.0 text input format
                        this.sendEvent(S2sEvent.contentStartTextUser(this.state.promptName, contentId));
                        this.sendEvent(S2sEvent.textInputContent(this.state.promptName, contentId, text));
                        this.sendEvent(S2sEvent.contentEnd(this.state.promptName, contentId));
                    }, 100);
                }
            };

            // Handle incoming messages
            this.socket.onmessage = (message) => {
                const event = JSON.parse(message.data);
                this.handleIncomingMessage(event);
            };

            // Handle errors
            this.socket.onerror = (error) => {
                console.error("WebSocket Error: ", error);
                this.setState({
                    websocketConnected: false,
                    alert: "Connection error occurred"
                });
            };

            // Handle connection close
            this.socket.onclose = (event) => {
                console.log("WebSocket Disconnected", event.code, event.reason);
                this.setState({
                    websocketConnected: false,
                    alert: event.code === 1000 ? null : "Connection lost"
                });
            };
        }
    }

    startSession() {
        // Start microphone - WebSocket connection is handled in handleSessionChange
        try {
            this.startMicrophone();
        } catch (error) {
            console.error('Error accessing microphone: ', error);
        }
    }

    async startMicrophone() {
        try {

            // Start microphone
            this.stream = await navigator.mediaDevices.getUserMedia({
                audio: {
                    channelCount: 1,           // Mono
                    sampleRate: 16000,         // 16kHz
                    sampleSize: 16,            // 16-bit
                    echoCancellation: true,    // Enable echo cancellation
                    noiseSuppression: true,    // Enable noise suppression
                    autoGainControl: true      // Enable automatic gain control
                }
            });

            // Create AudioContext for processing
            const audioContext = new AudioContext({
                sampleRate: 16000,
                latencyHint: 'interactive'
            });

            // Create MediaStreamSource
            const source = audioContext.createMediaStreamSource(this.stream);

            // Create ScriptProcessor for raw PCM data
            const processor = audioContext.createScriptProcessor(512, 1, 1);

            source.connect(processor);
            processor.connect(audioContext.destination);

            processor.onaudioprocess = (e) => {
                if (this.state.sessionStarted) {
                    const inputData = e.inputBuffer.getChannelData(0);

                    const buffer = new ArrayBuffer(inputData.length * 2);
                    const pcmData = new DataView(buffer);
                    for (let i = 0; i < inputData.length; i++) {
                        const int16 = Math.max(-32768, Math.min(32767, Math.round(inputData[i] * 32767)));
                        pcmData.setInt16(i * 2, int16, true);
                    }
                    // Binary data string
                    let data = "";
                    for (let i = 0; i < pcmData.byteLength; i++) {
                        data += String.fromCharCode(pcmData.getUint8(i));
                    }

                    // Send to WebSocket
                    const event = S2sEvent.audioInput(this.state.promptName, this.state.audioContentName, btoa(data));
                    this.sendEvent(event);
                }
            };

            // Store cleanup functions with stream reference
            window.audioCleanup = () => {
                processor.disconnect();
                source.disconnect();
            };

            this.mediaRecorder = new MediaRecorder(this.stream);
            this.mediaRecorder.ondataavailable = (event) => {
                this.state.audioChunks.push(event.data);
            };
            this.mediaRecorder.onstop = () => {
                const audioBlob = new Blob(this.state.audioChunks, { type: 'audio/wav' });

                // Send to WebSocket
                this.sendEvent(S2sEvent.audioInput(this.state.promptName, this.state.audioContentName, btoa(audioBlob)));
                this.setState({ audioChunks: [] });
            };

            this.mediaRecorder.start();
            this.setState({ sessionStarted: true });
        } catch (error) {
            console.error('Error accessing microphone: ', error);
        }

    }

    endSession() {
        // Stop microphone
        if (this.mediaRecorder && this.state.sessionStarted) {
            this.mediaRecorder.stop();
        }

        // Clean up audio context
        if (window.audioCleanup) {
            window.audioCleanup();
            window.audioCleanup = null;
        }

        // Stop all audio tracks to turn off microphone indicator
        if (this.stream) {
            this.stream.getTracks().forEach(track => {
                track.stop();
            });
            this.stream = null;
        }

        if (this.socket) {

            // Close S2sSessionManager
            this.sendEvent(S2sEvent.contentEnd(this.state.promptName, this.state.audioContentName));
            this.sendEvent(S2sEvent.promptEnd(this.state.promptName));
            this.sendEvent(S2sEvent.sessionEnd());

            // Close websocket
            this.socket.close();

            this.setState({ sessionStarted: false });
        }
    }

    handleTextInputChange(e) {
        this.setState({ textInput: e.target.value });
    }

    handleKeyPress(e) {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            this.handleTextSubmit();
        }
    }

    handleTextSubmit() {
        const text = this.state.textInput.trim();
        if (!text) return;

        // If not connected, start full session (WebSocket + microphone)
        if (!this.state.websocketConnected || !this.state.sessionStarted) {
            this.setState({
                chatMessages: {},
                audioResponse: {}
            });
            this.cancelAudio();
            
            // Store the text to send after connection
            this.pendingTextMessage = text;
            this.connectWebSocket(false); // Start full session with microphone
            this.setState({ textInput: '' });
            return;
        }

        // Send text message
        const contentId = crypto.randomUUID();
        
        // Add user message to chat immediately
        this.setState(prevState => ({
            chatMessages: {
                ...prevState.chatMessages,
                [contentId]: {
                    content: text,
                    role: "USER",
                    raw: [{ timestamp: Date.now(), event: { textOutput: { content: text, role: "USER", contentId } } }]
                }
            },
            textInput: '',
            isAssistantThinking: true
        }), () => {
            this.scrollChatToBottom();
        });

        // Send text input events to backend using Nova Sonic 2.0 format
        this.sendEvent(S2sEvent.contentStartTextUser(this.state.promptName, contentId));
        this.sendEvent(S2sEvent.textInputContent(this.state.promptName, contentId, text));
        this.sendEvent(S2sEvent.contentEnd(this.state.promptName, contentId));
    }

    render() {
        return (
            <div className="s2s">
                {this.state.alert && (
                    <Alert severity="error" className="error-alert">
                        {this.state.alert}
                    </Alert>
                )}

                {/* Voice Assistant Section */}
                <Box className="voice-assistant-container">
                    <div className="voice-assistant-section">
                        <div className="voice-assistant-header" onClick={this.toggleVoiceAssistant}>
                            <h3 className="voice-assistant-title">
                                <Icon name={this.state.voiceAssistantCollapsed ? "angle-right" : "angle-down"} />
                                &nbsp;&nbsp;
                                <svg width="20" height="20" viewBox="0 0 24 24" fill="#73BFA9" style={{ marginRight: '8px' }}>
                                    <path d="m19.46 8 .79-1.75L22 5.46c.39-.18.39-.73 0-.91l-1.75-.79L19.46 2c-.18-.39-.73-.39-.91 0l-.79 1.75-1.76.79c-.39.18-.39.73 0 .91l1.75.79.79 1.76c.18.39.74.39.92 0M11.5 9.5 9.91 6c-.35-.78-1.47-.78-1.82 0L6.5 9.5 3 11.09c-.78.36-.78 1.47 0 1.82l3.5 1.59L8.09 18c.36.78 1.47.78 1.82 0l1.59-3.5 3.5-1.59c.78-.36.78-1.47 0-1.82zm7.04 6.5-.79 1.75-1.75.79c-.39.18-.39.73 0 .91l1.75.79.79 1.76c.18.39.73.39.91 0l.79-1.75 1.76-.79c.39-.18.39-.73 0-.91l-1.75-.79-.79-1.76c-.18-.39-.74-.39-.92 0" />
                                </svg>
                                AI Assistant
                            </h3>
                        </div>
                        {!this.state.voiceAssistantCollapsed && (
                            <>
                                <div className="voice-assistant-content">
                                    <div className="chat-container-full">
                                        <div className="chatarea" ref={this.chatAreaRef}>
                                            {/* Render all messages in chronological order */}
                                            {Object.keys(this.state.chatMessages)
                                                .sort((a, b) => {
                                                    // Sort by creation order to maintain conversation flow
                                                    const msgA = this.state.chatMessages[a];
                                                    const msgB = this.state.chatMessages[b];
                                                    const timeA = msgA.raw && msgA.raw[0] ? msgA.raw[0].timestamp || 0 : 0;
                                                    const timeB = msgB.raw && msgB.raw[0] ? msgB.raw[0].timestamp || 0 : 0;
                                                    return timeA - timeB;
                                                })
                                                .map((key, index) => {
                                                    const msg = this.state.chatMessages[key];
                                                    // Show only speculative messages for fastest display, hide final to avoid duplicates
                                                    if (msg.role === "ASSISTANT" && msg.generationStage !== "SPECULATIVE") {
                                                        return null; // Hide final messages to avoid showing twice
                                                    }

                                                    // Render poster messages
                                                    if (msg.hasOwnProperty("poster")) {
                                                        return <div key={key} className="poster-container">
                                                            {msg["poster"]["content"].map((item, idx) => (
                                                                <div key={idx} className="it" onClick={() => {
                                                                    if (!item.content_url) return;
                                                                    this.setState({ playerVideoURL: item.content_url, voiceAssistantCollapsed: true });
                                                                    this.endSession();
                                                                    this.closeWebSocket();
                                                                }}>
                                                                    <img src={item.url} className="poster-image" alt={item.title} loading="lazy" />
                                                                    <br /><b>Title:</b> {item.title}<br />
                                                                    <b>Plot:</b> {item.plot}<br />
                                                                    <b>Rating:</b> <StarRating rating={item.rating} /><br />
                                                                </div>
                                                            ))}
                                                        </div>
                                                    }
                                                    // Render text messages
                                                    else if (msg.role || msg.content !== undefined) {
                                                        return <div key={key} className="item">
                                                            <div
                                                                className={msg.role === "USER" ? "user" : "bot"}
                                                                onClick={() => this.setState({
                                                                    showEventJson: true,
                                                                    selectedEvent: { events: msg.raw }
                                                                })}
                                                            >
                                                                {msg.role === "USER" ?
                                                                    <Icon name="user-profile" /> :
                                                                    <svg width="16" height="16" viewBox="0 0 24 24" fill="#73BFA9" style={{ marginRight: '4px' }}>
                                                                        <path d="m19.46 8 .79-1.75L22 5.46c.39-.18.39-.73 0-.91l-1.75-.79L19.46 2c-.18-.39-.73-.39-.91 0l-.79 1.75-1.76.79c-.39.18-.39.73 0 .91l1.75.79.79 1.76c.18.39.74.39.92 0M11.5 9.5 9.91 6c-.35-.78-1.47-.78-1.82 0L6.5 9.5 3 11.09c-.78.36-.78 1.47 0 1.82l3.5 1.59L8.09 18c.36.78 1.47.78 1.82 0l1.59-3.5 3.5-1.59c.78-.36.78-1.47 0-1.82zm7.04 6.5-.79 1.75-1.75.79c-.39.18-.39.73 0 .91l1.75.79.79 1.76c.18.39.73.39.91 0l.79-1.75 1.76-.79c.39-.18.39-.73 0-.91l-1.75-.79-.79-1.76c-.18-.39-.74-.39-.92 0" />
                                                                    </svg>
                                                                }
                                                                &nbsp;&nbsp;
                                                                {msg.content}
                                                            </div>

                                                        </div>
                                                    }
                                                    // Return null for any unhandled cases
                                                    return null;
                                                }).filter(Boolean)}

                                            {/* Empty state message when no chat messages */}
                                            {Object.keys(this.state.chatMessages).length === 0 && !this.state.isAssistantThinking && (
                                                <div className="empty-state-container">
                                                    <div className="empty-state-content">
                                                        <div className="ai-icon-container">
                                                            <img src={sonicGif} alt="Bedrock AI" height="180" />
                                                        </div>
                                                        <div className="empty-state-text">
                                                            <h3>AI ASSISTANT</h3>
                                                            <p>I can help you finding movies and recommendations<br />based on what you like.</p>
                                                            <p className="start-instruction">Click the mic or type a message to begin</p>
                                                        </div>
                                                    </div>
                                                </div>
                                            )}

                                            {/* Thinking animation */}
                                            {this.state.isAssistantThinking && (
                                                <div className="item">
                                                    <div className="bot thinking">
                                                        <svg width="16" height="16" viewBox="0 0 24 24" fill="#73BFA9" style={{ marginRight: '4px' }}>
                                                            <path d="m19.46 8 .79-1.75L22 5.46c.39-.18.39-.73 0-.91l-1.75-.79L19.46 2c-.18-.39-.73-.39-.91 0l-.79 1.75-1.76.79c-.39.18-.39.73 0 .91l1.75.79.79 1.76c.18.39.74.39.92 0M11.5 9.5 9.91 6c-.35-.78-1.47-.78-1.82 0L6.5 9.5 3 11.09c-.78.36-.78 1.47 0 1.82l3.5 1.59L8.09 18c.36.78 1.47.78 1.82 0l1.59-3.5 3.5-1.59c.78-.36.78-1.47 0-1.82zm7.04 6.5-.79 1.75-1.75.79c-.39.18-.39.73 0 .91l1.75.79.79 1.76c.18.39.73.39.91 0l.79-1.75 1.76-.79c.39-.18.39-.73 0-.91l-1.75-.79-.79-1.76c-.18-.39-.74-.39-.92 0" />
                                                        </svg>
                                                        &nbsp;&nbsp;
                                                        <span className="thinking-dots">
                                                            <span></span><span></span><span></span>
                                                        </span>
                                                    </div>
                                                </div>
                                            )}

                                            <div className="endbar" ref={this.chatMessagesEndRef}></div>
                                        </div>

                                        {/* New Input Section */}
                                        <div className="input-section">
                                            {/* Session Status and Controls */}
                                            <div className="session-status-bar">
                                                {this.state.sessionStarted && (
                                                    <div className="session-active-indicator">
                                                        <div className="pulse-dot"></div>
                                                        <span>Conversation Active</span>
                                                    </div>
                                                )}
                                                <div className="session-controls">
                                                    <button
                                                        className="icon-btn"
                                                        onClick={this.handleAudioMuteRequest}
                                                        disabled={!this.state.sessionStarted}
                                                        title={this.state.mute ? "Unmute microphone" : "Mute microphone"}
                                                    >
                                                        <Icon name={this.state.mute ? "microphone-off" : "microphone"} />
                                                        {this.state.mute ? "Unmute" : "Mute"}
                                                    </button>
                                                    <button
                                                        className="icon-btn"
                                                        onClick={() => this.setState({ showConfig: true })}
                                                        title="Settings"
                                                    >
                                                        <Icon name="settings" />
                                                        Settings
                                                    </button>
                                                    {this.state.sessionStarted && (
                                                        <button
                                                            className="icon-btn end-conversation-btn"
                                                            onClick={this.handleSessionChange}
                                                            title="End conversation"
                                                        >
                                                            <Icon name="close" />
                                                            End Conversation
                                                        </button>
                                                    )}
                                                </div>
                                            </div>

                                            {/* Voice Input - Primary */}
                                            <div className="voice-input-container">
                                                <button 
                                                    className={`mic-button ${this.state.sessionStarted ? 'active' : ''}`}
                                                    onClick={this.state.sessionStarted ? null : this.handleSessionChange}
                                                    disabled={this.state.sessionStarted}
                                                    title={this.state.sessionStarted ? "Microphone is active" : "Start conversation"}
                                                >
                                                    <svg width="32" height="32" viewBox="0 0 24 24" fill="currentColor">
                                                        <path d="M12 14c1.66 0 3-1.34 3-3V5c0-1.66-1.34-3-3-3S9 3.34 9 5v6c0 1.66 1.34 3 3 3zm5.91-3c-.49 0-.9.36-.98.85C16.52 14.2 14.47 16 12 16s-4.52-1.8-4.93-4.15c-.08-.49-.49-.85-.98-.85-.61 0-1.09.54-1 1.14.49 3 2.89 5.35 5.91 5.78V20c0 .55.45 1 1 1s1-.45 1-1v-2.08c3.02-.43 5.42-2.78 5.91-5.78.1-.6-.39-1.14-1-1.14z" />
                                                    </svg>
                                                </button>
                                                <div className="voice-input-label">
                                                    {this.state.sessionStarted ? "Microphone Active" : "Start Conversation"}
                                                </div>
                                            </div>

                                            {/* Divider */}
                                            <div className="input-divider">
                                                <span>or</span>
                                            </div>

                                            {/* Text Input - Secondary */}
                                            <div className="text-input-container">
                                                <TextField
                                                    fullWidth
                                                    multiline
                                                    maxRows={3}
                                                    placeholder="Type your question here..."
                                                    value={this.state.textInput}
                                                    onChange={this.handleTextInputChange}
                                                    onKeyPress={this.handleKeyPress}
                                                    variant="outlined"
                                                    className="text-input-field"
                                                    InputProps={{
                                                        endAdornment: (
                                                            <button
                                                                className="send-btn"
                                                                onClick={this.handleTextSubmit}
                                                                disabled={!this.state.textInput.trim()}
                                                                title="Send message"
                                                            >
                                                                <svg width="24" height="24" viewBox="0 0 24 24" fill="currentColor">
                                                                    <path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z" />
                                                                </svg>
                                                            </button>
                                                        )
                                                    }}
                                                />
                                            </div>
                                        </div>
                                    </div>
                                </div>
                                <audio ref={this.audioPlayerRef} preload="auto"></audio>
                            </>
                        )}
                    </div>
                </Box>

                {/* Hero Section */}
                <Box className="hero-container">
                    <Grid container className="hero-grid">
                        <Grid sm={12} md={12} lg={12} className="hero-video-grid">
                            <Box className="video-wrapper">
                                <ReactPlayer
                                    ref={this.videoPlayerRef}
                                    style={{ width: "100%", height: "40rem" }}
                                    controls
                                    autoPlay={true}
                                    muted={true}
                                    playing={this.state.isVideoPlaying}
                                    className="hero-video"
                                    src={this.state.playerVideoURL}
                                    onError={(e) => console.error('Video playback error:', e)}
                                    onReady={() => this.setState({ isVideoPlaying: true })}
                                    onPlay={() => {
                                        this.setState({ isVideoPlaying: true });
                                        this.endSession();
                                        this.closeWebSocket();
                                    }}
                                    onPause={() => this.setState({ isVideoPlaying: false })}
                                />
                            </Box>
                            {/* Movie Info underneath the video player */}
                            <Box className="movie-info-section">
                                <Typography variant="h4" className="hero-title">
                                    {this.getCurrentMovieInfo().title}
                                </Typography>
                                <Typography variant="subtitle1" className="hero-description">
                                    {this.getCurrentMovieInfo().description}
                                </Typography>
                            </Box>
                        </Grid>
                    </Grid>
                </Box>

                {/* Dialog */}
                <Dialog
                    open={this.state.showEventJson}
                    onClose={() => this.setState({ showEventJson: false })
                    }
                    maxWidth="md"
                    fullWidth
                    className="event-dialog"
                >
                    <DialogTitle>Event details</DialogTitle>
                    <DialogContent>
                        <div className="eventdetail">
                            <pre id="jsonDisplay">
                                {this.state.selectedEvent && this.state.selectedEvent.events.map((e, index) => {
                                    const eventType = Object.keys(e?.event)[0];
                                    if (eventType === "audioInput" || eventType === "audioOutput")
                                        e.event[eventType].content = e.event[eventType].content.substr(0, 10) + "...";
                                    const ts = new Date(e.timestamp).toLocaleString(undefined, {
                                        year: "numeric",
                                        month: "2-digit",
                                        day: "2-digit",
                                        hour: "2-digit",
                                        minute: "2-digit",
                                        second: "2-digit",
                                        fractionalSecondDigits: 3,
                                        hour12: false
                                    });
                                    var displayJson = { ...e };
                                    delete displayJson.timestamp;
                                    return (
                                        <div key={index}>
                                            {ts + "\n" + JSON.stringify(displayJson, null, 2) + "\n"}
                                        </div>
                                    );
                                })}
                            </pre>
                        </div>
                    </DialogContent>
                    <DialogActions>
                        <Button onClick={() => this.setState({ showEventJson: false })}>
                            Close
                        </Button>
                    </DialogActions>
                </Dialog>

                {/* Settings Modal */}
                <Dialog
                    open={this.state.showConfig}
                    onClose={() => this.setState({ showConfig: false })}
                    maxWidth="sm"
                    fullWidth
                >
                    <DialogTitle>Settings</DialogTitle>
                    <DialogContent>
                        <div style={{ marginTop: '16px' }}>
                            <FormControl fullWidth margin="normal">
                                <InputLabel>Voice</InputLabel>
                                <Select
                                    value={this.state.configVoiceIdOption.value}
                                    onChange={(e) => {
                                        const option = [
                                            { label: "Matthew (en-US)", value: "matthew" },
                                            { label: "Tiffany (en-US)", value: "tiffany" },
                                            { label: "Amy (en-GB)", value: "amy" }
                                        ].find(opt => opt.value === e.target.value);
                                        this.setState({ configVoiceIdOption: option });
                                    }}
                                >
                                    <MenuItem value="matthew">Matthew (en-US)</MenuItem>
                                    <MenuItem value="tiffany">Tiffany (en-US)</MenuItem>
                                    <MenuItem value="amy">Amy (en-GB)</MenuItem>
                                </Select>
                            </FormControl>
                            <TextField
                                fullWidth
                                multiline
                                rows={4}
                                margin="normal"
                                label="System Prompt"
                                value={this.state.configSystemPrompt}
                                onChange={(e) => this.setState({ configSystemPrompt: e.target.value })}
                                placeholder="Speech system prompt"
                            />
                        </div>
                    </DialogContent>
                    <DialogActions>
                        <Button onClick={() => this.setState({ showConfig: false })}>
                            Close
                        </Button>
                    </DialogActions>
                </Dialog>


            </div >
        );
    }

}

export default S2sChatBot;
