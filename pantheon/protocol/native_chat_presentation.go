package protocol

import _ "embed"

// NativeChatPresentation supplies retrieved setup and presentation instructions.
// Copied invitations link to discovery instead of duplicating these instructions.
// It is presentation guidance only, never proof of connection or readiness.
//
//go:embed native-chat-presentation.json
var NativeChatPresentation []byte
