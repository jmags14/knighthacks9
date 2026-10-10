/**
 * connectState(onState)
 *
 * Opens a WebSocket to /ws and calls onState(state) on every message.
 * Auto-reconnects every 2 s on close or error.
 *
 * While disconnected, onState is called once with { _connected: false }.
 * On every real message, state._connected === true is added for you.
 */
function connectState(onState) {
  var ws;

  function connect() {
    ws = new WebSocket("ws://" + location.host + "/ws");

    ws.onopen = function () {
      // Real state arrives via onmessage; dot update happens there
    };

    ws.onmessage = function (evt) {
      try {
        var state = JSON.parse(evt.data);
        state._connected = true;
        onState(state);
      } catch (e) {
        console.error("[state.js] bad JSON", e);
      }
    };

    ws.onclose = function () {
      onState({ _connected: false });
      setTimeout(connect, 2000);
    };

    ws.onerror = function () {
      // onclose fires right after onerror — reconnect handled there
    };
  }

  connect();
}
