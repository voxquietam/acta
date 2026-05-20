// Reactions bundle — registers the <emoji-picker> custom element.
//
// Importing the package runs ``customElements.define('emoji-picker', …)``
// as a side effect (see node_modules/emoji-picker-element/picker.js), so
// every <emoji-picker> in a reaction-bar popover upgrades automatically.
// The picker reads its emoji data from the locally-vendored
// ``static/vendor/emoji-data.json`` via each element's ``data-source``
// attribute — no runtime CDN fetch (matters on slow self-hosted uplinks).
//
// All open/close/positioning + the POST-on-pick wiring lives declaratively
// in templates/web/_reaction_bar.html (Alpine + HTMX); this file only
// needs to make the element exist. See
// docs/decisions/0014-frontend-architecture.md for the bundling rationale.
import "emoji-picker-element";
