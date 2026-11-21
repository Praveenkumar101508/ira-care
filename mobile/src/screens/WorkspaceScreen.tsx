import React from 'react';
import { Pressable, Text } from 'react-native';
import type { Workspace } from '../api/types';
import { Card, Muted, Screen } from '../ui';

export function WorkspaceScreen({ workspaces, onPick }: { workspaces: Workspace[]; onPick: (w: Workspace) => void }) {
  return (
    <Screen title="Choose a care circle">
      {workspaces.map((w) => (
        <Pressable key={w.id} onPress={() => onPick(w)} testID={`workspace-${w.id}`}>
          <Card>
            <Text style={{ fontSize: 18, fontWeight: '600' }}>{w.name}</Text>
            <Muted>{w.role.replace('_', ' ')}</Muted>
          </Card>
        </Pressable>
      ))}
      {workspaces.length === 0 ? <Muted>Your account is not in any care circle yet.</Muted> : null}
    </Screen>
  );
}
