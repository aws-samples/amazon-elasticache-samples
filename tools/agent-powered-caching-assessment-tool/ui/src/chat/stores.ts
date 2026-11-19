// Connection details and collection files the user gave in a form card. Held in this
// browser tab's memory only: never sent to the model, never persisted. The model gets
// an opaque id. A reload forgets them.
import type { LiveConnection, SavedCollection, Stores } from "./clientTools";

export class MemoryStores implements Stores {
  private readonly connections = new Map<string, LiveConnection>();
  private readonly collections = new Map<string, SavedCollection>();
  private n = 0;

  addConnection(c: LiveConnection): string {
    const id = `conn-${++this.n}`;
    this.connections.set(id, c);
    return id;
  }

  addCollection(c: SavedCollection): string {
    const id = `coll-${++this.n}`;
    this.collections.set(id, c);
    return id;
  }

  connection(id: string) { return this.connections.get(id); }
  collection(id: string) { return this.collections.get(id); }
}
