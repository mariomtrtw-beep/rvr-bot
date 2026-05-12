import React, { useEffect, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'

export default function AuthCallback() {
  const [status, setStatus] = useState('Processing...')
  const [searchParams] = useSearchParams()
  const navigate = useNavigate()

  useEffect(() => {
    const token = searchParams.get('token')
    
    if (token) {
      // Save token to localStorage
      localStorage.setItem('wordle-token', token)
      setStatus('✅ Authentication successful!')
      
      // Redirect to main app after 2 seconds
      setTimeout(() => {
        navigate('/', { replace: true })
      }, 2000)
    } else {
      setStatus('❌ No token received')
      setTimeout(() => {
        navigate('/', { replace: true })
      }, 3000)
    }
  }, [searchParams, navigate])

  return (
    <div className="min-h-screen bg-rvr-bg flex items-center justify-center">
      <div className="text-center">
        <div className="animate-spin rounded-full h-16 w-16 border-b-4 border-rvr-cyan mx-auto mb-6"></div>
        <h1 className="text-3xl font-bold text-rvr-cyan mb-4">
          {status}
        </h1>
        <p className="text-gray-400">
          Redirecting to RVR-Wordle...
        </p>
      </div>
    </div>
  )
}
