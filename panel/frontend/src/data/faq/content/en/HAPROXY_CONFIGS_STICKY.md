# Client stickiness

Needed when the application keeps the session in process memory: without stickiness the next request lands on another server and logs the user out.

## Two ways

| Method | How it works | Limitation |
|---|---|---|
| Cookie | The balancer adds its own cookie naming the server and reads it on later requests | HTTP traffic only |
| IP table | Stores "client address → server" for a set time | Clients behind one NAT all land on the same server |

## Good to know

- For stateless APIs and apps that keep sessions in a database or Redis, stickiness is unnecessary and only skews balancing.
- If the server a client is stuck to goes down, the connection moves elsewhere but the session is still lost: stickiness is not a replacement for shared session storage.
- Long sessions unbalance the pool: one server can end up holding noticeably more clients.
- Stickiness not working? Check that both the method and the entry lifetime are set, and that the config was reloaded.
