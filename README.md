# l2_cuak
- ALB + Target Group + Auto Scaling Group + Redis Setup is Complete (Follow Guides in agent_browser/src/agent_templates/browser_use_agent + This README.md)
- This l2_cuak codebase (browsing_agent.py here), NEEDS TO BE IMPROVED!!
      - Then pulled on all instances/CREATE ANOTHER AMI (v2)!!


### Some Sample Tasks (TO DO):
- Navigate to 'https://en.wikipedia.org/wiki/Internet' and scroll to the string 'The vast majority of computer'

### TO DO:
- Improve the base agent!!


### 03/22/2025- First stable version (v2)
- Redis global (across ALB) session routing
- Ensures stickiness and routing to the right VM inspite of intermediate calls (Vercel, etc.)
- Keeps the original setup plug and play 

### 03/21/2025- Containers retained for sessions per instance, sticky load balancer integrated
- Current ALB endpoints:
    - http://cuak-v1-stickiness-balancer-871735130.us-east-1.elb.amazonaws.com/start
    - http://cuak-v1-stickiness-balancer-871735130.us-east-1.elb.amazonaws.com/vnc/{SESSION_ID}
    - Ensure that you are calling it from the same client! (POSTMAN AND BROWSER are different clients)
    - Ensure the ALB has a rule to forward VNC to the same target group!! (STEPS BELOW)
    - Application-based stickiness cookie added for user access!!

### 03/20/2025- Chrome Containerized, Spawned based on Docker Image
- Current Instance's Static Endpoints:
    - http://44.195.135.191:8080/start 
    - http://44.195.135.191:8080/vnc/{SESSION_ID}


- The Chrome containers are managed dynamically (NEVER EXPOSED!)

1. Browser Use
- pipenv install (Dependencies added)



### ALB Rule Forwarding Guide (CUSTOM ENDPOINTS: /vnc/* example)
### ✅ Step-by-step: Setting up ALB WebSocket-friendly routing rules for `/vnc/{session_id}`

> By default, ALB should support WebSocket forwarding on HTTP/HTTPS listeners. But making it explicit helps ensure consistent routing behavior.

---

### 1️⃣ Go to your **Load Balancer** in the AWS Console:
- Navigate to **EC2 > Load Balancers**.
- Select your ALB: `cuak-v1-stickiness-balancer-...`.

---

### 2️⃣ Open the **Listeners** tab:
- Click on the HTTP:80 listener (or HTTPS:443 if you use SSL).

---

### 3️⃣ Edit listener **Rules**:
- Click **View/edit rules**.
- You’ll see the default rule forwarding all traffic to your target group.

---

### 4️⃣ Add a specific rule for WebSocket (optional but recommended):
- Click **Add rule**.
- Condition: 
  - **Path** → `starts with` → `/vnc/`
- Action: 
  - **Forward to target group** → `cuak-v1-target-group`.
- Make sure it’s prioritized above the default catch-all rule (or right after it).
- Click **Save**.

> ✅ ALB by default supports WebSocket over HTTP/HTTPS without special config, but this ensures routing consistency for `/vnc/{session_id}`.

---

### 5️⃣ Verify that:
- The default rule (`/`) also forwards to the same target group.
- Stickiness is enabled on that target group with a Load Balancer cookie.

---

### 6️⃣ Test routing consistency:
1. In your terminal:
   ```bash
   nslookup cuak-v1-stickiness-balancer-871735130.us-east-1.elb.amazonaws.com
   ```
   - Take note of the IP.

2. Start a session:
   ```bash
   curl -v http://cuak-v1-stickiness-balancer-871735130.us-east-1.elb.amazonaws.com/start
   ```
   - Look at the `X-Forwarded-For` and `X-Forwarded-Host` headers returned.  

3. Then:
   ```bash
   curl -v http://cuak-v1-stickiness-balancer-871735130.us-east-1.elb.amazonaws.com/vnc/<your-session-id>
   ```
   - Confirm both `/start` and `/vnc` hit the same backend instance (look for consistent headers or debug logs).