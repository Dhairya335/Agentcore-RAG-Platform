import { RefObject } from "react"
import { Message } from "./types"
import { ChatMessage } from "./ChatMessage"
import type { SourceViewerTarget } from "@/components/knowledge/SourceViewerDrawer"

interface ChatMessagesProps {
  messages:         Message[]
  messagesEndRef:   RefObject<HTMLDivElement | null>
  sessionId:        string
  onFeedbackSubmit: (messageContent: string, feedbackType: "positive" | "negative", comment: string) => Promise<void>
  onCitationClick?: (target: SourceViewerTarget) => void
}

export function ChatMessages({ messages, messagesEndRef, sessionId, onFeedbackSubmit, onCitationClick }: ChatMessagesProps) {
  return (
    <div className={`h-full p-4 space-y-4 w-full ${messages.length > 0 ? "overflow-y-auto" : "overflow-hidden"}`}>
      {messages.length === 0
        ? <div className="flex items-center justify-center h-full text-gray-400">Start a new conversation</div>
        : messages.map((message, index) => (
            <ChatMessage
              key={index}
              message={message}
              sessionId={sessionId}
              onFeedbackSubmit={async (feedbackType, comment) => onFeedbackSubmit(message.content, feedbackType, comment)}
              onCitationClick={onCitationClick}
            />
          ))
      }
      <div ref={messagesEndRef} />
    </div>
  )
}
