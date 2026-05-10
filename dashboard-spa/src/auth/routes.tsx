import { Navigate } from 'react-router-dom'
import type { ReactNode } from 'react'
import { useAuth } from './AuthContext'

interface RequireAuthProps {
  children: ReactNode
}

export function RequireAuth({ children }: RequireAuthProps) {
  const { state } = useAuth()
  if (!state) {
    return <Navigate to="/login" replace />
  }
  return <>{children}</>
}
