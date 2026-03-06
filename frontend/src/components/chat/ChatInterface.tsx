"use client"

import { useEffect, useRef, useState } from "react"
import { ChatHeader } from "./ChatHeader"
import { ChatInput } from "./ChatInput"
import { ChatMessages } from "./ChatMessages"
import { Message, MessageSegment, ToolCall } from "./types"

import { useGlobal } from "@/app/context/GlobalContext"
import { AgentCoreClient } from "@/lib/agentcore-client"
import type { AgentPattern } from "@/lib/agentcore-client"
import { submitFeedback } from "@/services/feedbackService"
import { useAuth } from "react-oidc-context"
import { useDefaultTool } from "@/hooks/useToolRenderer"
import { ToolCallDisplay } from "./ToolCallDisplay"
// ── NEW ────────────────────────────────────────────────────────────────────
import { DocumentUploadPanel } from "./DocumentUploadPanel"
import type { UploadedDocument } from "@/services/documentService"

export default function ChatInterface() {
  const [messages, setMessages] = useState<Message[]>([])
  const [input, setInput] = useState("")
  const [error, setError] = useState<string | null>(null)
  const [client, setClient] = useState<AgentCoreClient | null>(null)
  const [sessionId] = useState(() => crypto.randomUUID())
  // ── NEW: controls whether the upload panel is visible ─────────────────────
  const [isUploadPanelOpen, setIsUploadPanelOpen] = useState(false)

  const { isLoading, setIsLoading } = useGlobal()
  const auth = useAuth()

  const messagesEndRef = useRef<HTMLDivElement>(null)

  useDefaultTool(({ name, args, status, result }) => (
    <ToolCallDisplay name={name} args={args} status={status} result={result} />
  ))

  useEffect(() => {
    async function loadConfig() {
      try {
        const response = await fetch("/aws-exports.json")
        if (!response.ok) throw new Error("Failed to load configuration")
        const config = await response.json()
        if (!config.agentRuntimeArn) throw new Error("Agent Runtime ARN not found in configuration")

        const agentClient = new AgentCoreClient({
          runtimeArn: config.agentRuntimeArn,
          region: config.awsRegion || "us-east-1",
          pattern: (config.agentPattern || "strands-single-agent") as AgentPattern,
        })
        setClient(agentClient)
      } catch (err) {
        const errorMessage = err instanceof Error ? err.message : "Unknown error"
        setError(`Configuration error: ${errorMessage}`)
        console.error("Failed to load agent configuration:", err)
      }
    }
    loadConfig()
  }, [])

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" })
  }, [messages])

  const sendMessage = async (userMessage: string) => {
    if (!userMessage.trim() || !client) return

    setError(null)

    const newUserMessage: Message = {
      role: "user",
      content: userMessage,
      timestamp: new Date().toISOString(),
    }

    setMessages((prev) => [...prev, newUserMessage])
    setInput("")
    setIsLoading(true)

    const assistantResponse: Message = {
      role: "assistant",
      content: "",
      timestamp: new Date().toISOString(),
    }

    setMessages((prev) => [...prev, assistantResponse])

    try {
      const accessToken = auth.user?.access_token
      if (!accessToken) throw new Error("Authentication required. Please log in again.")

      const segments: MessageSegment[] = []
      const toolCallMap = new Map<string, ToolCall>()

      const updateMessage = () => {
        const content = segments
          .filter((s): s is Extract<MessageSegment, { type: "text" }> => s.type === "text")
          .map((s) => s.content)
          .join("")

        setMessages((prev) => {
          const updated = [...prev]
          updated[updated.length - 1] = {
            ...updated[updated.length - 1],
            content,
            segments: [...segments],
          }
          return updated
        })
      }

      await client.invoke(
        userMessage,
        sessionId,
        accessToken,
        (event) => {
          switch (event.type) {
            case "text": {
              const prev = segments[segments.length - 1]
              if (prev && prev.type === "tool") {
                for (const tc of toolCallMap.values()) {
                  if (tc.status === "streaming" || tc.status === "executing") {
                    tc.status = "complete"
                  }
                }
              }
              const last = segments[segments.length - 1]
              if (last && last.type === "text") {
                last.content += event.content
              } else {
                segments.push({ type: "text", content: event.content })
              }
              updateMessage()
              break
            }
            case "tool_use_start": {
              const tc: ToolCall = {
                toolUseId: event.toolUseId,
                name: event.name,
                input: "",
                status: "streaming",
              }
              toolCallMap.set(event.toolUseId, tc)
              segments.push({ type: "tool", toolCall: tc })
              updateMessage()
              break
            }
            case "tool_use_delta": {
              const tc = toolCallMap.get(event.toolUseId)
              if (tc) tc.input += event.input
              updateMessage()
              break
            }
            case "tool_result": {
              const tc = toolCallMap.get(event.toolUseId)
              if (tc) {
                tc.result = event.result
                tc.status = "complete"
              }
              updateMessage()
              break
            }
            case "message": {
              if (event.role === "assistant") {
                for (const tc of toolCallMap.values()) {
                  if (tc.status === "streaming") tc.status = "executing"
                }
                updateMessage()
              }
              break
            }
          }
        }
      )
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : "Unknown error"
      setError(`Failed to get response: ${errorMessage}`)
      console.error("Error invoking AgentCore:", err)
      setMessages((prev) => {
        const updated = [...prev]
        updated[updated.length - 1] = {
          ...updated[updated.length - 1],
          content: "I apologize, but I encountered an error processing your request. Please try again.",
        }
        return updated
      })
    } finally {
      setIsLoading(false)
    }
  }

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    sendMessage(input)
  }

  const handleFeedbackSubmit = async (
    messageContent: string,
    feedbackType: "positive" | "negative",
    comment: string
  ) => {
    try {
      const idToken = auth.user?.id_token
      if (!idToken) throw new Error("Authentication required. Please log in again.")
      await submitFeedback({ sessionId, message: messageContent, feedbackType, comment: comment || undefined }, idToken)
      console.log("Feedback submitted successfully")
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : "Unknown error"
      console.error("Error submitting feedback:", err)
      setError(`Failed to submit feedback: ${errorMessage}`)
    }
  }

  const startNewChat = () => {
    setMessages([])
    setInput("")
    setError(null)
    setIsUploadPanelOpen(false)
  }

  // ── NEW: when upload succeeds, close panel and notify user in chat ─────────
  const handleUploadSuccess = (doc: UploadedDocument) => {
    setIsUploadPanelOpen(false)
    // Add a system-style message so user knows what was uploaded
    const uploadMsg: Message = {
      role: "user",
      content: `📎 Uploaded: **${doc.fileName}** (v${doc.version}) — processing will begin shortly. You can ask me questions about it once it's ready.`,
      timestamp: new Date().toISOString(),
    }
    setMessages((prev) => [...prev, uploadMsg])
  }

  const isInitialState = messages.length === 0
  const hasAssistantMessages = messages.some((m) => m.role === "assistant")

  // ── Shared input area (used in both initial and chat-in-progress states), extract it here so upload panel always appears directly above input
  const inputArea = (
    <>
      {/* ── NEW: Upload panel slides in above the input bar ─────────────── */}
      {isUploadPanelOpen && (
        <DocumentUploadPanel
          onClose={() => setIsUploadPanelOpen(false)}
          onUploadSuccess={handleUploadSuccess}
        />
      )}
      <ChatInput
        input={input}
        setInput={setInput}
        handleSubmit={handleSubmit}
        isLoading={isLoading}
        // ── NEW props ────────────────────────────────────────────────────
        onUploadClick={() => setIsUploadPanelOpen((prev) => !prev)}
        isUploadPanelOpen={isUploadPanelOpen}
      />
    </>
  )

  return (
    <div className="flex flex-col h-screen w-full">
      {/* Fixed header */}
      <div className="flex-none">
        <ChatHeader onNewChat={startNewChat} canStartNewChat={hasAssistantMessages} />
        {error && (
          <div className="bg-red-50 border-l-4 border-red-500 p-4 mx-4 mt-2">
            <p className="text-sm text-red-700">{error}</p>
          </div>
        )}
      </div>

      {isInitialState ? (
        <>
          <div className="grow" />
          <div className="text-center mb-6">
            <h2 className="text-2xl font-bold text-gray-800">Welcome to FAST Chat</h2>
            <p className="text-gray-600 mt-2">Ask me anything to get started</p>
          </div>
          <div className="px-0 mb-16 max-w-4xl mx-auto w-full">
            {inputArea}
          </div>
          <div className="grow" />
        </>
      ) : (
        <>
          <div className="grow overflow-hidden">
            <div className="max-w-4xl mx-auto w-full h-full">
              <ChatMessages
                messages={messages}
                messagesEndRef={messagesEndRef}
                sessionId={sessionId}
                onFeedbackSubmit={handleFeedbackSubmit}
              />
            </div>
          </div>
          <div className="flex-none">
            <div className="max-w-4xl mx-auto w-full">
              {inputArea}
            </div>
          </div>
        </>
      )}
    </div>
  )
}