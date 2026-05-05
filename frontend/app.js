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

    const viewReportBtn = document.getElementById('view-report-btn');
    const downloadRobotBtn = document.getElementById('download-robot-btn');
    const downloadDocxBtn = document.getElementById('download-docx-btn');
    const copyCodeBtn = document.getElementById('copy-code-btn');
    const rfCodeDisplay = document.getElementById('rf-code-display');
    const failedTestsList = document.getElementById('failed-tests-list');

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
    const steps = ['parse', 'generate', 'validate', 'execute'];

    function resetPipeline() {
        steps.forEach(s => {
            const el = document.getElementById(`step-${s}`);
            el.classList.remove('active', 'done', 'error');
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

    function setStepError(stepName, detail) {
        const el = document.getElementById(`step-${stepName}`);
        el.classList.remove('active');
        el.classList.add('error');
        document.getElementById(`step-${stepName}-status`).textContent = detail || '❌';
    }

    // ── Escape HTML ──
    function escapeHTML(str) {
        if (!str) return '';
        return str.toString().replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
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

        // Animate pipeline steps with delays
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

            if (data.status === 'completed') {
                setStepDone('execute', `✅ ${data.execution.passed}/${data.execution.total} passés`);
            } else if (data.status === 'error') {
                setStepError('execute', '❌ Erreur');
            } else {
                setStepDone('execute', '✅ Terminé');
            }

            // Update results
            const exec = data.execution || {};
            metricTotal.textContent = exec.total || 0;
            metricPassed.textContent = exec.passed || 0;
            metricFailed.textContent = exec.failed || 0;

            // Show failed tests
            if (exec.failed_tests && exec.failed_tests.length > 0) {
                failedTestsList.innerHTML = '<h3 style="margin-bottom:0.5rem;color:#ef4444;">❌ Tests échoués :</h3>';
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
                } else if (!el.classList.contains('done')) {
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
