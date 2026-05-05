document.addEventListener('DOMContentLoaded', () => {
    const runBtn = document.getElementById('run-btn');
    const urlInput = document.getElementById('url-input');
    const markdownInput = document.getElementById('markdown-input');
    const markdownFile = document.getElementById('markdown-file');
    const fileNameDisplay = document.getElementById('file-name');
    const statusText = document.getElementById('agent-status');

    const pipelinePanel = document.getElementById('pipeline-panel');
    const resultsPanel = document.getElementById('results-panel');
    const codePanel = document.getElementById('code-panel');

    const metricTotal = document.getElementById('metric-total');
    const metricPassed = document.getElementById('metric-passed');
    const metricFailed = document.getElementById('metric-failed');
    const metricHealed = document.getElementById('metric-healed');
    const metricHealedWrap = document.getElementById('metric-healed-wrap');

    const viewReportBtn = document.getElementById('view-report-btn');
    const downloadRobotBtn = document.getElementById('download-robot-btn');
    const downloadDocxBtn = document.getElementById('download-docx-btn');
    const copyCodeBtn = document.getElementById('copy-code-btn');
    const rfCodeDisplay = document.getElementById('rf-code-display');
    const failedTestsList = document.getElementById('failed-tests-list');
    const testDetailsList = document.getElementById('test-details-list');

    let lastTestName = null;

    // ── File Upload Handler ──
    markdownFile.addEventListener('change', (e) => {
        if (e.target.files.length > 0) {
            const file = e.target.files[0];
            fileNameDisplay.textContent = file.name;
            fileNameDisplay.closest('.file-label').classList.add('has-file');
            const reader = new FileReader();
            reader.onload = (event) => {
                markdownInput.value = event.target.result;
            };
            reader.readAsText(file);
        } else {
            fileNameDisplay.textContent = 'Choisir un fichier .md';
            fileNameDisplay.closest('.file-label').classList.remove('has-file');
            markdownInput.value = '';
        }
    });

    // ── UI State ──
    const setLoading = (isLoading) => {
        const btnText = runBtn.querySelector('.btn-text');
        const spinner = runBtn.querySelector('.spinner');
        runBtn.disabled = isLoading;
        urlInput.disabled = isLoading;
        markdownInput.disabled = isLoading;

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

    // ── Pipeline Steps ──
    const steps = ['parse', 'generate', 'validate', 'execute', 'healing'];

    function resetPipeline() {
        steps.forEach(s => {
            const el = document.getElementById(`step-${s}`);
            el.classList.remove('active', 'done', 'error', 'healed');
            document.getElementById(`step-${s}-status`).textContent = '';
        });
    }

    function setStepActive(stepName) {
        const el = document.getElementById(`step-${stepName}`);
        el.classList.add('active');
        document.getElementById(`step-${stepName}-status`).textContent = '⏳ En cours...';
    }

    function setStepDone(stepName, detail) {
        const el = document.getElementById(`step-${stepName}`);
        el.classList.remove('active');
        el.classList.add('done');
        document.getElementById(`step-${stepName}-status`).textContent = detail || '✅';
    }

    function setStepHealed(stepName, detail) {
        const el = document.getElementById(`step-${stepName}`);
        el.classList.remove('active');
        el.classList.add('healed');
        document.getElementById(`step-${stepName}-status`).textContent = detail || '🔧';
    }

    function setStepError(stepName, detail) {
        const el = document.getElementById(`step-${stepName}`);
        el.classList.remove('active');
        el.classList.add('error');
        document.getElementById(`step-${stepName}-status`).textContent = detail || '❌';
    }

    function setStepSkipped(stepName) {
        const el = document.getElementById(`step-${stepName}`);
        el.classList.remove('active');
        document.getElementById(`step-${stepName}-status`).textContent = '— Skipped';
    }

    // ── Escape HTML ──
    function escapeHTML(str) {
        if (!str) return '';
        return str.toString().replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

    // ── Build test details with healing info ──
    function renderTestDetails(data) {
        const exec = data.execution || {};
        const healing = data.healing || {};
        const healedTests = healing.healed_tests || [];
        const healingAttempts = healing.healing_attempts || {};
        const stillFailing = healing.still_failing || [];
        const passedTests = exec.passed_tests || [];
        const failedTests = exec.failed_tests || [];

        testDetailsList.innerHTML = '';

        // Build a list of all test names
        const allTests = [];

        // Add passed tests
        passedTests.forEach(name => {
            const tcId = name.match(/TC-?\d+/i)?.[0] || name;
            const isHealed = healedTests.includes(tcId);
            allTests.push({ name, tcId, status: isHealed ? 'healed' : 'passed', attempts: healingAttempts[tcId] || 0 });
        });

        // Add failed tests
        failedTests.forEach(entry => {
            const name = entry.split(':')[0].trim();
            const tcId = name.match(/TC-?\d+/i)?.[0] || name;
            allTests.push({ name, tcId, status: 'failed', attempts: healingAttempts[tcId] || 0 });
        });

        if (allTests.length === 0) return;

        const heading = document.createElement('h3');
        heading.style.marginBottom = '0.75rem';
        heading.style.fontSize = '1rem';
        heading.textContent = '🧪 Détail par Test';
        testDetailsList.appendChild(heading);

        allTests.forEach(tc => {
            const item = document.createElement('div');
            item.className = `test-detail-item test-${tc.status}`;

            let badge = '';
            let icon = '';
            if (tc.status === 'passed') {
                icon = '✅';
                badge = '<span class="test-badge badge-passed">PASSED</span>';
            } else if (tc.status === 'healed') {
                icon = '🔧';
                badge = `<span class="test-badge badge-healed">HEALED (${tc.attempts} attempt${tc.attempts > 1 ? 's' : ''})</span>`;
            } else {
                icon = '❌';
                const attText = tc.attempts > 0 ? ` (${tc.attempts} attempt${tc.attempts > 1 ? 's' : ''})` : '';
                badge = `<span class="test-badge badge-failed">FAILED${attText}</span>`;
            }

            item.innerHTML = `${icon} ${escapeHTML(tc.name)} ${badge}`;
            testDetailsList.appendChild(item);
        });

        testDetailsList.classList.remove('hidden');
    }

    // ── Run Pipeline ──
    runBtn.addEventListener('click', async () => {
        const markdownContent = markdownInput.value.trim();
        const baseUrl = urlInput.value.trim();

        if (!markdownContent) {
            statusText.textContent = '❌ Veuillez fournir un fichier .md ou du contenu markdown.';
            return;
        }

        if (!baseUrl) {
            statusText.textContent = '❌ Veuillez saisir l\'URL de base.';
            return;
        }

        setLoading(true);
        statusText.textContent = 'Pipeline en cours...';

        // Animate pipeline steps
        setStepActive('parse');

        try {
            const response = await fetch('/api/generate-rf', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    markdown_content: markdownContent,
                    base_url: baseUrl
                })
            });

            const data = await response.json();

            if (!response.ok) {
                throw new Error(data.detail || 'Server error');
            }

            // Update pipeline visuals
            setStepDone('parse', `✅ ${data.test_cases_parsed || 0} TC(s)`);
            setStepDone('generate', `✅ Généré`);

            if (data.validation && data.validation.valid) {
                setStepDone('validate', '✅ Valide');
            } else {
                setStepError('validate', '⚠️ Warnings');
            }

            // Execution step
            const exec = data.execution || {};
            const healing = data.healing || {};
            const healedCount = (healing.healed_tests || []).length;
            const stillFailingCount = (healing.still_failing || []).length;

            if (data.status === 'completed') {
                setStepDone('execute', `✅ ${exec.passed}/${exec.total} passés`);
            } else if (data.status === 'error') {
                setStepError('execute', '❌ Erreur');
            } else {
                setStepDone('execute', '✅ Terminé');
            }

            // Healing step
            if (healedCount > 0 || stillFailingCount > 0) {
                if (healedCount > 0 && stillFailingCount === 0) {
                    setStepHealed('healing', `🔧 ${healedCount} test(s) guéri(s)`);
                } else if (healedCount > 0) {
                    setStepHealed('healing', `🔧 ${healedCount} guéri(s), ❌ ${stillFailingCount} échoué(s)`);
                } else {
                    setStepError('healing', `❌ ${stillFailingCount} non guéri(s)`);
                }
            } else {
                setStepSkipped('healing');
            }

            // Update results metrics
            metricTotal.textContent = exec.total || 0;
            metricPassed.textContent = exec.passed || 0;
            metricFailed.textContent = exec.failed || 0;

            // Show healed metric
            if (healedCount > 0) {
                metricHealed.textContent = healedCount;
                metricHealedWrap.style.display = '';
            } else {
                metricHealedWrap.style.display = 'none';
            }

            // Render per-test details with healing info
            renderTestDetails(data);

            // Show failed tests (legacy list)
            if (exec.failed_tests && exec.failed_tests.length > 0) {
                failedTestsList.innerHTML = '<h3 style="margin-bottom:0.5rem;color:#ef4444;">❌ Tests échoués (après healing) :</h3>';
                exec.failed_tests.forEach(ft => {
                    const div = document.createElement('div');
                    div.className = 'failed-test-item';
                    div.textContent = ft;
                    failedTestsList.appendChild(div);
                });
                failedTestsList.classList.remove('hidden');
            } else {
                failedTestsList.classList.add('hidden');
            }

            // Show RF code
            if (data.rf_code) {
                rfCodeDisplay.textContent = data.rf_code;
                codePanel.classList.remove('hidden');
            }

            // Store test name for report/download
            lastTestName = data.test_name;

            // Show action buttons
            if (data.report_path) {
                viewReportBtn.classList.remove('hidden');
            }
            if (data.robot_file) {
                downloadRobotBtn.classList.remove('hidden');
            }
            if (data.docx_path) {
                downloadDocxBtn.classList.remove('hidden');
            }

            resultsPanel.classList.remove('hidden');
            statusText.textContent = '✅ Pipeline terminé.';

        } catch (error) {
            console.error(error);
            statusText.textContent = `❌ Erreur : ${error.message}`;

            // Mark remaining steps as error
            steps.forEach(s => {
                const el = document.getElementById(`step-${s}`);
                if (el.classList.contains('active')) {
                    setStepError(s, '❌');
                } else if (!el.classList.contains('done') && !el.classList.contains('healed')) {
                    el.classList.add('error');
                    document.getElementById(`step-${s}-status`).textContent = '—';
                }
            });
        } finally {
            setLoading(false);
        }
    });

    // ── View Report ──
    viewReportBtn.addEventListener('click', () => {
        if (lastTestName) {
            window.open(`/api/report/${lastTestName}`, '_blank');
        }
    });

    // ── Download .robot ──
    downloadRobotBtn.addEventListener('click', () => {
        if (lastTestName) {
            window.open(`/api/download/${lastTestName}`, '_blank');
        }
    });

    // ── Download DOCX ──
    downloadDocxBtn.addEventListener('click', () => {
        if (lastTestName) {
            window.open(`/api/download-docx/${lastTestName}`, '_blank');
        }
    });

    // ── Copy Code ──
    copyCodeBtn.addEventListener('click', async () => {
        const code = rfCodeDisplay.textContent;
        if (!code) return;

        try {
            await navigator.clipboard.writeText(code);
            copyCodeBtn.textContent = '✅ Copié !';
            copyCodeBtn.classList.add('copied');
            setTimeout(() => {
                copyCodeBtn.textContent = '📋 Copier';
                copyCodeBtn.classList.remove('copied');
            }, 2000);
        } catch {
            // Fallback
            const textarea = document.createElement('textarea');
            textarea.value = code;
            document.body.appendChild(textarea);
            textarea.select();
            document.execCommand('copy');
            document.body.removeChild(textarea);
            copyCodeBtn.textContent = '✅ Copié !';
            setTimeout(() => { copyCodeBtn.textContent = '📋 Copier'; }, 2000);
        }
    });
});
