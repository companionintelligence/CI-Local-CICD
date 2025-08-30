#!/bin/bash

# Quick setup verification for CI-Server monitoring
echo "=== CI-Server Setup Verification ==="

# Check if repository exists
if [ -d "$HOME/devel/CI-Server" ]; then
    echo "✅ CI-Server repository found"
    cd "$HOME/devel/CI-Server"
    
    # Check if it's a git repo
    if [ -d ".git" ]; then
        echo "✅ Valid Git repository"
        
        # Show current status
        echo ""
        echo "📊 Repository Info:"
        echo "Current branch: $(git branch --show-current)"
        echo "Current commit: $(git rev-parse HEAD | cut -c1-8)"
        
        # Fetch latest and compare
        git fetch > /dev/null 2>&1
        LOCAL=$(git rev-parse HEAD)
        REMOTE=$(git rev-parse origin/main)
        
        if [ "$LOCAL" = "$REMOTE" ]; then
            echo "✅ Up to date with origin/main"
        else
            echo "⚠️  Behind origin/main"
            echo "Local:  $(echo $LOCAL | cut -c1-8)"
            echo "Remote: $(echo $REMOTE | cut -c1-8)"
        fi
    else
        echo "❌ Not a Git repository"
        exit 1
    fi
else
    echo "❌ CI-Server repository not found at ~/devel/CI-Server"
    echo ""
    echo "To set up, run:"
    echo "  mkdir -p ~/devel"
    echo "  cd ~/devel"
    echo "  git clone https://github.com/companionintelligence/CI-Server"
    exit 1
fi

echo ""
echo "🚀 Ready to start monitoring!"
echo "Run: python3 test_ci_server_daemon.py"