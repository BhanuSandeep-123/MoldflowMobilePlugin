// Define steps and phases structure
const workflow = [
  { id: 'project-setup', name: 'Create Project', phase: 'Phase 1: Project Setup', progress: 8 },
  { id: 'import-cad', name: 'Import CAD', phase: 'Phase 1: Project Setup', progress: 16 },
  { id: 'cad-diagnostics', name: 'CAD Diagnostics', phase: 'Phase 1: Project Setup', progress: 24 },
  { id: 'analysis-sequence', name: 'Analysis Sequence', phase: 'Phase 2: Analysis Setup', progress: 32 },
  { id: 'material-selection', name: 'Material Selection', phase: 'Phase 2: Analysis Setup', progress: 40 },
  { id: 'process-settings', name: 'Process Settings', phase: 'Phase 2: Analysis Setup', progress: 48 },
  { id: 'gate-selection', name: 'Gate Selection', phase: 'Phase 2: Analysis Setup', progress: 56 },
  { id: 'mesh-generation', name: 'Mesh Generation', phase: 'Phase 3: Simulation', progress: 64 },
  { id: 'mesh-diagnostics', name: 'Mesh Diagnostics', phase: 'Phase 3: Simulation', progress: 72 },
  { id: 'run-analysis', name: 'Run Analysis', phase: 'Phase 3: Simulation', progress: 80 },
  { id: 'review-results', name: 'Review Results', phase: 'Phase 4: Reporting', progress: 88 },
  { id: 'generate-report', name: 'Generate Report', phase: 'Phase 4: Reporting', progress: 95 },
  { id: 'complete', name: 'Complete', phase: 'Workflow Completed', progress: 100 }
];

let currentIndex = 0;
let paused = false;
let pollingInterval = null;

// Elements
const stepperEl = document.getElementById('stepper');
const workspacePages = document.querySelectorAll('.workspace-page');
const timelineLogEl = document.getElementById('timeline-log');

// Controls
const btnPrev = document.getElementById('btn-prev');
const btnContinue = document.getElementById('btn-continue');
const btnPause = document.getElementById('btn-pause');
const btnResume = document.getElementById('btn-resume');
const btnCancel = document.getElementById('btn-cancel');

// Header elements
const headerPhaseText = document.getElementById('header-phase-text');
const headerTimeRemaining = document.getElementById('header-time-remaining');
const headerProgressFill = document.getElementById('header-progress-fill');
const headerProjectName = document.getElementById('header-project-name');
const systemStatusText = document.getElementById('system-status-text');
const systemStatusIndicator = document.querySelector('.status-indicator');

// Right panel elements
const rightProject = document.getElementById('summary-project-name');
const rightCad = document.getElementById('summary-cad-file');
const rightAnalysis = document.getElementById('summary-analysis-seq');
const rightMaterial = document.getElementById('summary-material');
const rightTemps = document.getElementById('summary-temperatures');
const rightGate = document.getElementById('summary-gate');
const rightMesh = document.getElementById('summary-mesh');
const rightStudy = document.getElementById('summary-study-name');
const rightElapsed = document.getElementById('summary-elapsed');
const rightPhase = document.getElementById('summary-phase');

function renderStepper(state) {
  stepperEl.innerHTML = '';
  let currentPhase = '';
  
  workflow.forEach((step, idx) => {
    if (step.phase !== currentPhase) {
      currentPhase = step.phase;
      const phaseHeader = document.createElement('div');
      phaseHeader.className = 'stepper-phase';
      phaseHeader.textContent = currentPhase.toUpperCase();
      stepperEl.appendChild(phaseHeader);
    }
    
    const stepDiv = document.createElement('div');
    stepDiv.className = `stepper-step ${step.id === state.current_step ? 'active' : ''}`;
    
    const indicator = document.createElement('span');
    indicator.className = 'step-indicator';
    
    // Assign status class based on active step status
    const currentStepIndex = workflow.findIndex(w => w.id === state.current_step);
    if (idx < currentStepIndex) {
      indicator.classList.add('completed');
    } else if (idx === currentStepIndex) {
      if (state.step_status === "Waiting") {
        indicator.classList.add('waiting');
      } else {
        indicator.classList.add('running');
      }
    } else {
      indicator.classList.add('pending');
    }
    
    const label = document.createElement('span');
    label.className = 'step-label';
    label.textContent = step.name;
    
    stepDiv.appendChild(indicator);
    stepDiv.appendChild(label);
    stepperEl.appendChild(stepDiv);
  });
}

function renderTimeline(logs) {
  timelineLogEl.innerHTML = '';
  logs.forEach(log => {
    const item = document.createElement('div');
    item.className = 'timeline-item';
    
    const time = document.createElement('span');
    time.className = 'timeline-time';
    time.textContent = log.time;
    
    const status = document.createElement('span');
    status.className = 'timeline-status';
    status.textContent = log.status;
    
    const text = document.createElement('span');
    text.className = 'timeline-text';
    text.textContent = log.text;
    
    item.appendChild(time);
    item.appendChild(status);
    item.appendChild(text);
    timelineLogEl.appendChild(item);
  });
  timelineLogEl.scrollTop = timelineLogEl.scrollHeight;
}

function showPromptOverlay(prompt) {
  // Remove existing prompt overlay if any
  const existing = document.getElementById('prompt-overlay');
  if (existing) existing.remove();

  const overlay = document.createElement('div');
  overlay.id = 'prompt-overlay';
  overlay.style.position = 'fixed';
  overlay.style.top = '0';
  overlay.style.left = '0';
  overlay.style.width = '100vw';
  overlay.style.height = '100vh';
  overlay.style.backgroundColor = 'rgba(0,0,0,0.4)';
  overlay.style.display = 'flex';
  overlay.style.alignItems = 'center';
  overlay.style.justifyAlignment = 'center';
  overlay.style.justifyContent = 'center';
  overlay.style.zIndex = '9999';

  const box = document.createElement('div');
  box.className = 'card';
  box.style.width = '420px';
  box.style.padding = '24px';
  box.style.boxShadow = '0 8px 32px rgba(0,0,0,0.2)';
  box.style.borderRadius = '8px';
  box.style.display = 'flex';
  box.style.flexDirection = 'column';
  box.style.gap = '16px';

  const title = document.createElement('h3');
  title.textContent = prompt.title;
  title.style.margin = '0';
  title.style.fontFamily = 'Outfit, sans-serif';

  const msg = document.createElement('p');
  msg.textContent = prompt.message;
  msg.style.color = '#5f6368';
  msg.style.fontSize = '13px';
  msg.style.lineHeight = '1.5';

  const btnContainer = document.createElement('div');
  btnContainer.style.display = 'flex';
  btnContainer.style.gap = '8px';
  btnContainer.style.justifyContent = 'flex-end';

  prompt.options.forEach(opt => {
    const btn = document.createElement('button');
    btn.className = opt === "Yes" || opt === "Continue" ? 'btn btn-primary' : 'btn btn-secondary';
    btn.textContent = opt;
    btn.onclick = () => respondToPrompt(opt);
    btnContainer.appendChild(btn);
  });

  box.appendChild(title);
  box.appendChild(msg);
  box.appendChild(btnContainer);
  overlay.appendChild(box);
  document.body.appendChild(overlay);
}

function hidePromptOverlay() {
  const existing = document.getElementById('prompt-overlay');
  if (existing) existing.remove();
}

async function respondToPrompt(answer) {
  hidePromptOverlay();
  try {
    await fetch('/api/respond', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ answer })
    });
  } catch (e) {
    console.error("Failed to respond to prompt:", e);
  }
}

// State Polling Loop
async function pollState() {
  try {
    const res = await fetch('/ui_state.json', { cache: 'no-store' });
    if (!res.ok) return;
    const state = await res.json();

    // 1. Sync Workspace page
    workspacePages.forEach(p => p.classList.remove('active'));
    const activePage = document.getElementById(`page-${state.current_step}`);
    if (activePage) {
      activePage.classList.add('active');
    }

    // 2. Sync Stepper & Logs
    renderStepper(state);
    renderTimeline(state.activity_log);

    // 3. Sync Header info
    headerProjectName.textContent = state.params.project_name;
    const activeStepObj = workflow.find(w => w.id === state.current_step);
    if (activeStepObj) {
      headerPhaseText.textContent = activeStepObj.phase;
      headerProgressFill.style.width = `${activeStepObj.progress}%`;
    }
    headerTimeRemaining.textContent = `Remaining: ${state.remaining_time}`;

    // Status message
    if (state.step_status === "Waiting") {
      systemStatusIndicator.className = 'status-indicator waiting';
      systemStatusText.textContent = 'Waiting for User Input';
    } else {
      systemStatusIndicator.className = 'status-indicator running';
      systemStatusText.textContent = 'Working Automatically';
    }

    // 4. Sync Right summary panel
    rightProject.textContent = state.params.project_name;
    rightCad.textContent = state.params.cad_file;
    rightAnalysis.textContent = state.params.analysis_sequence;
    rightMaterial.textContent = state.params.material;
    rightTemps.textContent = state.params.temperatures;
    rightGate.textContent = state.params.gate_status;
    rightMesh.textContent = state.params.mesh_status;
    rightStudy.textContent = state.params.study_name;
    rightElapsed.textContent = state.elapsed_time;
    rightPhase.textContent = state.params.phase;

    // 5. Render custom progress states based on active screen
    if (state.current_step === "mesh-generation") {
      const ring = document.getElementById('mesh-ring');
      if (ring) {
        const offset = 502 - (502 * state.mesh_progress.percentage) / 100;
        ring.style.strokeDashoffset = offset;
        document.querySelector('#page-mesh-generation .ring-label').textContent = `${state.mesh_progress.percentage}%`;
        document.querySelector('#page-mesh-generation .sim-details').innerHTML = `
          <div class="sim-row"><span>Task:</span><strong>${state.mesh_progress.task}</strong></div>
          <div class="sim-row"><span>Elapsed Time:</span><strong>${state.elapsed_time}</strong></div>
          <div class="sim-row"><span>Remaining Time:</span><strong>${state.remaining_time}</strong></div>
        `;
      }
    } else if (state.current_step === "run-analysis") {
      const ring = document.getElementById('solver-ring');
      if (ring) {
        const offset = 502 - (502 * state.solver_progress.percentage) / 100;
        ring.style.strokeDashoffset = offset;
        document.querySelector('#page-run-analysis .ring-label').textContent = `${state.solver_progress.percentage}%`;
        document.querySelector('#page-run-analysis .sim-details').innerHTML = `
          <div class="sim-row"><span>Solver:</span><strong>${state.solver_progress.phase}</strong></div>
          <div class="sim-row"><span>Task:</span><strong>${state.solver_progress.task}</strong></div>
          <div class="sim-row"><span>Elapsed:</span><strong>${state.elapsed_time}</strong></div>
        `;
      }
    }

    // 6. Handle Prompts
    if (state.pending_prompt) {
      showPromptOverlay(state.pending_prompt);
    } else {
      hidePromptOverlay();
    }

  } catch (e) {
    console.error("Error polling state:", e);
  }
}

// Start polling
pollingInterval = setInterval(pollState, 500);
pollState();
