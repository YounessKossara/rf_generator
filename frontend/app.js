document.addEventListener('DOMContentLoaded', () => {
    // ── Element refs ──
    const runBtn            = document.getElementById('run-btn');
    const urlInput          = document.getElementById('url-input');
    const markdownInput     = document.getElementById('markdown-input');
    const markdownFile      = document.getElementById('markdown-file');
    const fileNameDisplay   = document.getElementById('file-name');
    const statusText        = document.getElementById('agent-status');

    const pipelinePanel     = document.getElementById('pipeline-panel');
    const resultsPanel      = document.getElementById('results-panel');
    const codePanel         = document.getElementById('code-panel');
    const executeActionRow  = document.getElementById('execute-action-row');
    const cmdlineHint       = document.getElementById('cmdline-hint');
    const cmdlineHintCode   = document.getElementById('cmdline-hint-code');

    const metricTotal       = document.getElementById('metric-total');
    const metricPassed      = document.getElementById('metric-passed');
    const metricFailed      = document.getElementById('metric-failed');
    const metricHealed      = document.getElementById('metric-healed');
    const metricHealedWrap  = document.getElementById('metric-healed-wrap');

    const viewReportBtn     = document.getElementById('view-report-btn');
    const downloadRobotBtn  = document.getElementById('download-robot-btn');
    const downloadDocxBtn   = document.getElementById('download-docx-btn');
    const copyCodeBtn       = document.getElementById('copy-code-btn');
    const rfCodeDisplay     = document.getElementById('rf-code-display');
    const testDetailsList   = document.getElementById('test-details-list');
    const failedTestsList   = document.getElementById('failed-tests-list');

    let lastTestName    = null;
    let testCasesData   = [];
    let currentBaseUrl  = '';
    let selectedMode    = null;   // 'generate-only' | 'generate-execute'

    // ── Mode selection ──────────────────────────────────────────────────────
    document.querySelectorAll('.mode-card').forEach(card => {
        card.addEventListener('click', () => {
            document.querySelectorAll('.mode-card').forEach(c => c.classList.remove('selected'));
            card.classList.add('selected');
            selectedMode = card.dataset.mode;
            runBtn.disabled = false;
            statusText.textContent = selectedMode === 'generate-only'
                ? 'Mode : Generate Only — prêt.'
                : 'Mode : Generate & Execute — prêt.';
        });
    });

    // ── UI state helpers ────────────────────────────────────────────────────
    const setGenerateLoading = (isLoading) => {
        const btnText = runBtn.querySelector('.btn-text');
        const spinner = runBtn.querySelector('.spinner');
        runBtn.disabled = isLoading;
        if (isLoading) {
            btnText.classList.add('hidden');
            spinner.classList.remove('hidden');
            resultsPanel.classList.add('hidden');
            codePanel.classList.add('hidden');
            pipelinePanel.classList.remove('hidden');
            resetPipeline();
        } else {
            btnText.classList.remove('hidden');
            spinner.classList.add('hidden');
        }
    };

    const setExecuteLoading = (isLoading) => {
        const executeBtn = document.getElementById('execute-btn');
        const btnText    = executeBtn.querySelector('.exec-btn-text');
        const spinner    = executeBtn.querySelector('.exec-spinner');
        executeBtn.disabled = isLoading;
        if (isLoading) {
            btnText.classList.add('hidden');
            spinner.classList.remove('hidden');
            statusText.textContent = 'Exécution en cours...';
        } else {
            btnText.classList.remove('hidden');
            spinner.classList.add('hidden');
        }
    };

    // ── Pipeline steps ──────────────────────────────────────────────────────
    const steps = ['parse', 'generate', 'validate', 'execute', 'healing'];

    function resetPipeline() {
        steps.forEach(s => {
            const el = document.getElementById(`step-${s}`);
            el.classList.remove('active', 'done', 'error', 'healed');
            document.getElementById(`step-${s}-status`).textContent = '';
        });
    }

    function setStepActive(s)          { const el = document.getElementById(`step-${s}`); el.classList.add('active'); document.getElementById(`step-${s}-status`).textContent = '⏳ En cours...'; }
    function setStepDone(s, detail)    { const el = document.getElementById(`step-${s}`); el.classList.remove('active'); el.classList.add('done');   document.getElementById(`step-${s}-status`).textContent = detail || '✅'; }
    function setStepHealed(s, detail)  { const el = document.getElementById(`step-${s}`); el.classList.remove('active'); el.classList.add('healed'); document.getElementById(`step-${s}-status`).textContent = detail || '🔧'; }
    function setStepError(s, detail)   { const el = document.getElementById(`step-${s}`); el.classList.remove('active'); el.classList.add('error');  document.getElementById(`step-${s}-status`).textContent = detail || '❌'; }
    function setStepSkipped(s)         { document.getElementById(`step-${s}-status`).textContent = '— Skipped'; }

    // ── Helpers ─────────────────────────────────────────────────────────────
    function escapeHTML(str) {
        if (!str) return '';
        return str.toString().replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

    function renderTestDetails(data) {
        const exec          = data.execution || {};
        const healing       = data.healing   || {};
        const healedTests   = healing.healed_tests    || [];
        const healingAttempts = healing.healing_attempts || {};
        const passedTests   = exec.passed_tests || [];
        const failedTests   = exec.failed_tests || [];

        testDetailsList.innerHTML = '';
        const allTests = [];

        passedTests.forEach(name => {
            const tcId    = name.match(/TC-?\d+/i)?.[0] || name;
            const isHealed = healedTests.includes(tcId);
            allTests.push({ name, tcId, status: isHealed ? 'healed' : 'passed', attempts: healingAttempts[tcId] || 0 });
        });

        failedTests.forEach(entry => {
            const name  = entry.split(':')[0].trim();
            const tcId  = name.match(/TC-?\d+/i)?.[0] || name;
            allTests.push({ name, tcId, status: 'failed', attempts: healingAttempts[tcId] || 0 });
        });

        if (allTests.length === 0) return;

        const heading = document.createElement('h3');
        heading.textContent = '🧪 Détail par Test';
        testDetailsList.appendChild(heading);

        allTests.forEach(tc => {
            const item  = document.createElement('div');
            item.className = `test-detail-item test-${tc.status}`;
            const icon  = tc.status === 'failed' ? '❌' : tc.status === 'healed' ? '🔧' : '✅';
            const badge = tc.status === 'passed'  ? 'PASSED'
                        : tc.status === 'healed'  ? `HEALED (${tc.attempts})`
                        : 'FAILED';
            item.innerHTML = `${icon} ${escapeHTML(tc.name)} <span class="test-badge badge-${tc.status}">${badge}</span>`;
            testDetailsList.appendChild(item);
        });

        testDetailsList.classList.remove('hidden');
    }

    // ── Show code panel in the right mode ───────────────────────────────────
    function showCodePanel(rfCode, testName) {
        rfCodeDisplay.value = rfCode;
        codePanel.classList.remove('hidden');
        lastTestName = testName;
        document.getElementById('download-gen-robot-btn').classList.remove('hidden');

        if (selectedMode === 'generate-only') {
            // Hide execute button, show cmdline hint
            executeActionRow.classList.add('hidden');
            const safeTestName = testName;
            cmdlineHintCode.textContent =
                `robot --outputdir output/rf_reports/${safeTestName}` +
                ` --variable SCREENSHOT_ROOT:output/screenshots` +
                ` output/robot_files/${safeTestName}.robot`;
            cmdlineHint.classList.remove('hidden');
        } else {
            // Show execute button, hide cmdline hint
            executeActionRow.classList.remove('hidden');
            cmdlineHint.classList.add('hidden');
        }
    }

    // ── Step 1: GENERATE ────────────────────────────────────────────────────
    runBtn.addEventListener('click', async () => {
        if (!selectedMode) {
            statusText.textContent = '❌ Veuillez choisir un mode.';
            return;
        }
        const markdownContent = markdownInput.value.trim();
        const baseUrl         = urlInput.value.trim();

        if (!markdownContent || !baseUrl) {
            statusText.textContent = '❌ Veuillez remplir le markdown et l\'URL.';
            return;
        }

        setGenerateLoading(true);
        statusText.textContent = 'Génération en cours...';
        setStepActive('parse');

        try {
            const response = await fetch('/api/generate-rf', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ markdown_content: markdownContent, base_url: baseUrl }),
            });

            const data = await response.json();
            if (!response.ok) throw new Error(data.detail || 'Server error');

            testCasesData  = data.test_cases || [];
            currentBaseUrl = data.base_url;

            setStepDone('parse',    `✅ ${data.test_cases_parsed} TC(s)`);
            setStepDone('generate', '✅ Généré');

            if (data.validation?.valid) setStepDone('validate', '✅ Valide');
            else                        setStepError('validate', '⚠️ Warnings');

            showCodePanel(data.rf_code, data.test_name);

            if (selectedMode === 'generate-only') {
                // Done — user will run manually
                statusText.textContent = '✅ Code généré. Téléchargez le .robot et exécutez-le manuellement.';
                setStepSkipped('execute');
                setStepSkipped('healing');
                setGenerateLoading(false);
            } else {
                // Auto-execute immediately
                statusText.textContent = '⚙️ Lancement de l\'exécution automatique...';
                setGenerateLoading(false);
                await runExecute(data.rf_code, data.test_name);
            }
        } catch (error) {
            statusText.textContent = `❌ Erreur : ${error.message}`;
            setStepError('parse');
            setGenerateLoading(false);
        }
    });

    // ── Execute (called directly or by auto-chain) ───────────────────────────
    async function runExecute(rfCode, testName) {
        setExecuteLoading(true);
        setStepActive('execute');

        try {
            const response = await fetch('/api/execute-rf', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    rf_code:    rfCode,
                    base_url:   currentBaseUrl,
                    test_cases: testCasesData,
                    test_name:  testName || lastTestName || '',
                }),
            });

            const data = await response.json();
            if (!response.ok) throw new Error(data.detail || 'Server error');

            const exec        = data.execution || {};
            const healing     = data.healing   || {};
            const healedCount = (healing.healed_tests || []).length;

            setStepDone('execute', `✅ ${exec.passed}/${exec.total} passés`);
            if (healedCount > 0) setStepHealed('healing', `🔧 ${healedCount} guéri(s)`);
            else                 setStepSkipped('healing');

            metricTotal.textContent  = exec.total  || 0;
            metricPassed.textContent = exec.passed || 0;
            metricFailed.textContent = exec.failed || 0;

            if (healedCount > 0) {
                metricHealed.textContent = healedCount;
                metricHealedWrap.style.display = '';
            }

            renderTestDetails(data);

            lastTestName = data.test_name;
            if (data.report_path) viewReportBtn.classList.remove('hidden');
            if (data.robot_file)  downloadRobotBtn.classList.remove('hidden');
            if (data.docx_path)   downloadDocxBtn.classList.remove('hidden');

            resultsPanel.classList.remove('hidden');
            statusText.textContent = '✅ Exécution terminée.';
        } catch (error) {
            statusText.textContent = `❌ Erreur d'exécution : ${error.message}`;
            setStepError('execute');
        } finally {
            setExecuteLoading(false);
        }
    }

    // ── Manual execute button (Generate & Execute mode only) ─────────────────
    document.getElementById('execute-btn').addEventListener('click', async () => {
        const rfCode = rfCodeDisplay.value.trim();
        if (!rfCode) return;
        await runExecute(rfCode, lastTestName);
    });

    // ── File upload ──────────────────────────────────────────────────────────
    markdownFile.addEventListener('change', (e) => {
        if (e.target.files.length > 0) {
            const file = e.target.files[0];
            fileNameDisplay.textContent = file.name;
            fileNameDisplay.closest('.file-label').classList.add('has-file');
            const reader = new FileReader();
            reader.onload = (event) => { markdownInput.value = event.target.result; };
            reader.readAsText(file);
        } else {
            fileNameDisplay.textContent = 'Choisir un fichier .md';
            fileNameDisplay.closest('.file-label').classList.remove('has-file');
            markdownInput.value = '';
        }
    });

    // ── Download / report buttons ────────────────────────────────────────────
    document.getElementById('download-gen-robot-btn').addEventListener('click', () => {
        if (lastTestName) window.open(`/api/download/${lastTestName}`, '_blank');
    });
    viewReportBtn.addEventListener('click',    () => lastTestName && window.open(`/api/report/${lastTestName}`, '_blank'));
    downloadRobotBtn.addEventListener('click', () => lastTestName && window.open(`/api/download/${lastTestName}`, '_blank'));
    downloadDocxBtn.addEventListener('click',  () => lastTestName && window.open(`/api/download-docx/${lastTestName}`, '_blank'));
    copyCodeBtn.addEventListener('click', () => {
        navigator.clipboard.writeText(rfCodeDisplay.value);
        copyCodeBtn.textContent = '✅ Copié !';
        setTimeout(() => copyCodeBtn.textContent = '📋 Copier', 2000);
    });
});
