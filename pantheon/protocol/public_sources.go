package protocol

import _ "embed"

// QualifiedPublicSources maps internal package identities to accessible public
// snapshots. It contains only already public package metadata, never Git history.
//
//go:embed qualified-public-sources.json
var QualifiedPublicSources []byte
