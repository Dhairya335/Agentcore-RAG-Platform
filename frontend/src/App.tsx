// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { BrowserRouter } from 'react-router-dom'
import { AuthProvider } from '@/components/auth/AuthProvider'
import { SessionBootstrapProvider } from '@/app/context/SessionContext'
import AppRoutes from './routes'

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        {/* SessionBootstrapProvider must be inside AuthProvider (needs OIDC context)
            but outside AppRoutes (provides session state to all routes). */}
        <SessionBootstrapProvider>
          <AppRoutes />
        </SessionBootstrapProvider>
      </AuthProvider>
    </BrowserRouter>
  )
}
