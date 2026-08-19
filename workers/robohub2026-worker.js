addEventListener('fetch', event => {
  event.respondWith(handleRequest(event.request));
});

async function handleRequest(request) {
  const url = new URL(request.url);
  const target = new URL(
    'https://robohub2026.onrender.com' + url.pathname + url.search
  );

  const headers = new Headers();
  for (const [key, value] of request.headers) {
    if (key.toLowerCase() === 'host') continue;
    headers.append(key, value);
  }

  const newRequest = new Request(target, {
    method: request.method,
    headers: headers,
    body: request.body,
  });

  const response = await fetch(newRequest);
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers: response.headers,
  });
}
