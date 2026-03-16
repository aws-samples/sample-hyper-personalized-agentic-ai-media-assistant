class S2sEvent {
    static DEFAULT_INFER_CONFIG = {
      maxTokens: 1024,
      topP: 0.95,
      temperature: 0.7
    };

    // Defining system prompt and tool use configuration on the client side is only for demo purposes.
    // In production, these definitions should be defined on the server side. 
    
    static DEFAULT_SYSTEM_PROMPT = `You are a friendly movie assistant. You specialize in helping users with movie recommendations or answering questions about specific movie scene.
    You must appear highly charismatic, and speak in a friendly tone like a good friend, also being funny.
    When you greet a user, you should be charismatic but be brief and use only a few words.
    The user and you will engage in a spoken dialog exchanging the transcripts of a natural real-time conversation.
    The users are looking for assistance in movie recommendation, or asking about the details in a particular scene of a show he is currently watching.
    Keep your responses short, generally two or three sentences for chatty scenarios.
    Only use a single pair of square brackets for indicating a stage command.
    You should only help with movie recommedations, and explaining plots for the given shows.
    If user asks about a movie scene, you can assume it's the movie the user is currently watching on the screen. No need to ask for the movie name.
    If user asks about movie or tv show recommendations, you must use 'getMovieRecommendation' tool for the recommendation. Do not recommend any other movies. Only use getMovieRecommendation for movies or shows, do not use this tool for answering any questions about the actors or anything other than the movies.
    If user asks about specific detail in a movie or a TV show, such as scenes, you must use the 'explainTheMovieScene' tool to get the explaination. Do not provide explaination using your own knowledge.
    If user asks about who are the actors in a movie, you should use 'getActorInformation' tool to find the correct answer for the user.
    If you are not sure whether the question is about the movie scene he is watching, or about movie recommendation, you should ask for clarification. Do not assume anything if you are not sure.
    You must not answer anything that's not about movies. Do not ask personal information such as name, contact or address.
    
    
    # Here are some examples of questions and the tool selection:
    ## Question: Tell me what's going on in this scene?
    Tool: explainTheMovieScene
    ## Question: I am looking for fun movies, what would you recommend?
    Tool: getMovieRecommendation
    ## Question: Tell me what's going on in this scene?
    Tool: explainTheMovieScene
    ## Question: In this movie schene, why is she getting married?
    Tool: explainTheMovieScene
    ## Question: Who are the actors in XYZ?
    Tool: getActorInformation
    ## Question: I am looking for XYZ, do you have it in your collection?
    Tool: getMovieRecommendation
    ## Question: Do you have XYZ?
    Tool: getMovieRecommendation
    

    When responding to users about the recommended movies, your response must just be a brief sentence without mentioning any recommended titles or plots, because the information is to be provided separately so we don't need to repeat. For example, you can respond with: "Here are the recommendation shows, let me know if there is anything else I could help". 
    If user asks for movie recommendation of a franchise, or specific actor/actress, your must not assume the recommended titles have the exact match. For instance, if user asks "Do you have John Smith's movie?", rather than saying 'here are some movies featuring John Smith' with high confidence, your response should be generic like: "Here are my recommendations featuring John Smith or something similar". Notice the 'or something similar' at the end of the sentence indicates the recommended titles might not have the perfect match but similar ones are being recommended.`;

    static DEFAULT_AUDIO_INPUT_CONFIG = {
      mediaType: "audio/lpcm",
      sampleRateHertz: 16000,
      sampleSizeBits: 16,
      channelCount: 1,
      audioType: "SPEECH",
      encoding: "base64"
    };
  
    static DEFAULT_AUDIO_OUTPUT_CONFIG = {
      mediaType: "audio/lpcm",
      sampleRateHertz: 24000,
      sampleSizeBits: 16,
      channelCount: 1,
      voiceId: "matthew",
      encoding: "base64",
      audioType: "SPEECH"
    };
  
    static DEFAULT_TOOL_CONFIG = {
      tools: [
      {
        toolSpec: {
          name: "getMovieRecommendation",
          description: "Get recommendation for movies or shows based on plots, genres or titles",
          inputSchema: {
            json: JSON.stringify({
              "$schema": "http://json-schema.org/draft-07/schema#",
              type: "object",
              properties: {
                question: {
                    type: "string",
                    description: "user question about the movie recommendation"
                  }  
              },
              required: [ "question"]
            })
          }
        }
      },
      {
        toolSpec: {
          name: "explainTheMovieScene",
          description: "Explain a plot or a scene in a movie or show",
          inputSchema: {
            json: JSON.stringify({
              "$schema": "http://json-schema.org/draft-07/schema#",
              type: "object",
              properties: {
                question: {
                    type: "string",
                    description: "user question about the scene or plot in a movie"
                  }  
              },
              required: [ "question"]
            })
          }
        }
      },
      {
        "toolSpec": {
        "name": "getActorInformation",
        "description": "Get information about actors in a given show",
        inputSchema: {
            json: JSON.stringify({
              "$schema": "http://json-schema.org/draft-07/schema#",
              type: "object",
              properties: {
                question: {
                    type: "string",
                    description: "user question about the actors in a movie or tv show"
                  }  
              },
              required: [ "question"]
            })
          }
        }
    },
    ]
    };
  
    static sessionStart(inferenceConfig = S2sEvent.DEFAULT_INFER_CONFIG) {
      return { event: { sessionStart: { inferenceConfiguration: inferenceConfig },
                        turnDetectionConfiguration: {
                          endpointingSensitivity: "LOW"
                        }
                    }
             };
    }
  
    static promptStart(promptName, audioOutputConfig = S2sEvent.DEFAULT_AUDIO_OUTPUT_CONFIG, toolConfig = S2sEvent.DEFAULT_TOOL_CONFIG) {
      return {
        "event": {
          "promptStart": {
            "promptName": promptName,
            "textOutputConfiguration": {
              "mediaType": "text/plain"
            },
            "audioOutputConfiguration": audioOutputConfig,
          
          "toolUseOutputConfiguration": {
            "mediaType": "application/json"
          },
          "toolConfiguration": toolConfig
        }
        }
      }
    }
  
    static contentStartText(promptName, contentName) {
      return {
        "event": {
          "contentStart": {
            "promptName": promptName,
            "contentName": contentName,
            "type": "TEXT",
            "interactive": true,
            "role": "SYSTEM",
            "textInputConfiguration": {
              "mediaType": "text/plain"
            }
          }
        }
      }
    }

    static contentStartTextUser(promptName, contentName) {
      return {
        "event": {
          "contentStart": {
            "promptName": promptName,
            "contentName": contentName,
            "type": "TEXT",
            "interactive": true,
            "role": "USER",
            "textInputConfiguration": {
              "mediaType": "text/plain"
            }
          }
        }
      };
    }

    static textInputContent(promptName, contentName, content) {
      return {
        event: {
          textInput: {
            "promptName": promptName,
            "contentName": contentName,
            "content": content,
          }
        }
      };
    }
  
    static textInput(promptName, contentName, voiceId, systemPrompt = S2sEvent.DEFAULT_SYSTEM_PROMPT) {
      let voiceIdName = voiceId.split(" ")[0]
      let sysPrompt = systemPrompt.slice();
      const voiceNameIntro = `Your name is ${voiceIdName}. `
      const sysPromptWithVoiceId = voiceNameIntro + sysPrompt // Change the first occurrence of the voice name.

      return {
        "event": {
          "textInput": {
            "promptName": promptName,
            "contentName": contentName,
            "content": sysPromptWithVoiceId
          }
        }
      }
    }
  
    static contentEnd(promptName, contentName) {
      return {
        "event": {
          "contentEnd": {
            "promptName": promptName,
            "contentName": contentName
          }
        }
      }
    }
  
    static contentStartAudio(promptName, contentName, audioInputConfig = S2sEvent.DEFAULT_AUDIO_INPUT_CONFIG) {
      return {
        "event": {
          "contentStart": {
            "promptName": promptName,
            "contentName": contentName,
            "type": "AUDIO",
            "interactive": true,
            "role": "USER",
            "audioInputConfiguration": {
              "mediaType": "audio/lpcm",
              "sampleRateHertz": 16000,
              "sampleSizeBits": 16,
              "channelCount": 1,
              "audioType": "SPEECH",
              "encoding": "base64"
            }
          }
        }
      }
    }
  
    static audioInput(promptName, contentName, content) {
      return {
        event: {
          audioInput: {
            promptName,
            contentName,
            content,
          }
        }
      };
    }
  
    static contentStartTool(promptName, contentName, toolUseId) {
      return {
        event: {
          contentStart: {
            promptName,
            contentName,
            interactive: false,
            type: "TOOL",
            toolResultInputConfiguration: {
              toolUseId,
              type: "TEXT",
              textInputConfiguration: { mediaType: "text/plain" }
            }
          }
        }
      };
    }
  
    static textInputTool(promptName, contentName, content) {
      return {
        event: {
          textInput: {
            promptName,
            contentName,
            content,
            role: "TOOL"
          }
        }
      };
    }
  
    static promptEnd(promptName) {
      return {
        event: {
          promptEnd: {
            promptName
          }
        }
      };
    }
  
    static sessionEnd() {
      return { event: { sessionEnd: {} } };
    }
  }
  export default S2sEvent;