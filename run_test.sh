#!/bin/bash

echo "=== Running CI-Server Monitor Test ==="
echo ""

# Make scripts executable
chmod +x test_ci_server_daemon.py
chmod +x deploy_ci_monitor.py
chmod +x verify_setup.sh

echo "📋 Step 1: Verifying setup..."
./verify_setup.sh

if [ $? -eq 0 ]; then
    echo ""
    echo "📋 Step 2: Running daemon test..."
    python3 test_ci_server_daemon.py
    
    echo ""
    echo "📋 Step 3: Ready for deployment!"
    echo "Run: python3 deploy_ci_monitor.py"
else
    echo "❌ Setup verification failed. Please fix the issues above."
fi