import { Button } from "@/components/ui/button"
import { Plus, BookOpen } from "lucide-react"
import { useAuth } from "@/hooks/useAuth"
import { useIsInternal } from "@/hooks/useUserRole"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog"

type ChatHeaderProps = {
  title?: string | undefined
  onNewChat: () => void
  canStartNewChat: boolean
  onLibraryOpen?: () => void
}

export function ChatHeader({ title, onNewChat, canStartNewChat, onLibraryOpen }: ChatHeaderProps) {
  const { isAuthenticated, signOut } = useAuth()
  const isInternal = useIsInternal()

  return (
    <header className="flex items-center justify-between p-4 border-b w-full">
      <div className="flex items-center gap-2">
        {isInternal && onLibraryOpen && (
          <Button variant="ghost" size="icon" onClick={onLibraryOpen} title="Knowledge Base" className="text-gray-500 hover:text-blue-600">
            <BookOpen className="h-5 w-5" />
          </Button>
        )}
        <h1 className="text-xl font-bold">{title || "Fullstack AgentCore Solution Template"}</h1>
      </div>
      <div className="flex items-center gap-2">
        <Button onClick={onNewChat} variant="outline" className="gap-2" disabled={!canStartNewChat}>
          <Plus className="h-4 w-4" />
          New Chat
        </Button>
        {isAuthenticated && (
          <AlertDialog>
            <AlertDialogTrigger asChild>
              <Button variant="outline">Logout</Button>
            </AlertDialogTrigger>
            <AlertDialogContent>
              <AlertDialogHeader>
                <AlertDialogTitle>Confirm Logout</AlertDialogTitle>
                <AlertDialogDescription>
                  Are you sure you want to log out? You will need to sign in again to access your
                  account.
                </AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter>
                <AlertDialogCancel>Cancel</AlertDialogCancel>
                <AlertDialogAction onClick={() => signOut()}>Confirm</AlertDialogAction>
              </AlertDialogFooter>
            </AlertDialogContent>
          </AlertDialog>
        )}
      </div>
    </header>
  )
}
